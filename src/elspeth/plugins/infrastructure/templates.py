"""Shared Jinja2 template infrastructure for transform plugins.

Provides a sandboxed Jinja2 environment factory and the TemplateError exception.
Used by both LLM prompt templates and RAG query templates.

The sandbox prevents unsafe access. Constant folding of authored expressions
is disabled during bounded-size compilation; rendering runs in a child process
with CPU, memory, input and output ceilings.
"""

from __future__ import annotations

import math
import multiprocessing
import os
import pickle
import resource
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from jinja2 import StrictUndefined, Template, TemplateSyntaxError, nodes
from jinja2.compiler import CodeGenerator
from jinja2.exceptions import SecurityError, TemplateRuntimeError, UndefinedError
from jinja2.meta import find_undeclared_variables
from jinja2.sandbox import ImmutableSandboxedEnvironment
from jinja2.visitor import NodeVisitor

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.core.templates import validate_jinja_source


class TemplateError(Exception):
    """Error in template rendering (including sandbox violations)."""


_MAX_RENDER_BYTES = 4 * 1024 * 1024
_MAX_CONTEXT_BYTES = 8 * 1024 * 1024
# RLIMIT_AS is virtual address space, including the interpreter's existing
# mappings. Cap growth from the spawned worker's own baseline, not an absolute
# address size that depends on which web/test modules Python imported.
_MAX_WORKER_ADDRESS_GROWTH = 256 * 1024 * 1024
_WORKER_TIMEOUT_SECONDS = 5.0
_WORKER_SLOTS = threading.BoundedSemaphore(2)


class _NoFoldCodeGenerator(CodeGenerator):
    """Never evaluate an authored expression while compiling a template."""

    def _output_child_to_const(self, node: nodes.Expr, frame: Any, finalize: Any) -> str:
        if type(node) is nodes.TemplateData:
            return super()._output_child_to_const(node, frame, finalize)
        raise nodes.Impossible()


class _LocalSandboxedEnvironment(ImmutableSandboxedEnvironment):
    code_generator_class = _NoFoldCodeGenerator


@dataclass(frozen=True)
class _RowTransport:
    data: bytes
    contract: bytes


def _pack_context_value(value: Any, *, depth: int = 0) -> Any:
    """Detach owned frozen row carriers before the worker's pickle boundary."""
    from elspeth.contracts.schema_contract import PipelineRow

    if depth > 64:
        raise TemplateError("Template context nesting exceeds 64 levels")
    if type(value) is PipelineRow:
        return _RowTransport(pickle.dumps(value.to_dict(), protocol=5), pickle.dumps(value.contract.to_checkpoint_format(), protocol=5))
    if type(value) in (dict, MappingProxyType):
        return {key: _pack_context_value(item, depth=depth + 1) for key, item in value.items()}
    if type(value) is list:
        return [_pack_context_value(item, depth=depth + 1) for item in value]
    if type(value) is tuple:
        return tuple(_pack_context_value(item, depth=depth + 1) for item in value)
    return value


def _restore_context_value(value: Any) -> Any:
    if type(value) is _RowTransport:
        from elspeth.contracts.schema_contract import PipelineRow, SchemaContract

        return PipelineRow(pickle.loads(value.data), SchemaContract.from_checkpoint(pickle.loads(value.contract)))
    if type(value) is dict:
        return {key: _restore_context_value(item) for key, item in value.items()}
    if type(value) is list:
        return [_restore_context_value(item) for item in value]
    if type(value) is tuple:
        return tuple(_restore_context_value(item) for item in value)
    return value


def _check_template_source(source: str) -> None:
    try:
        validate_jinja_source(source)
    except ValueError as exc:
        raise TemplateError(str(exc)) from exc


def _template_worker(connection: Any, source: str, payload: bytes) -> None:
    """Render in a process with an OS memory/CPU ceiling."""
    try:
        baseline_pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[0])
        max_address_space = baseline_pages * os.sysconf("SC_PAGE_SIZE") + _MAX_WORKER_ADDRESS_GROWTH
        resource.setrlimit(resource.RLIMIT_AS, (max_address_space, max_address_space))
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_limit = math.ceil(usage.ru_utime + usage.ru_stime + 2)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
        context = pickle.loads(payload)
        if type(context) is not dict or any(type(key) is not str for key in context):
            raise TemplateError("Template worker received an invalid context")
        context = _restore_context_value(context)
        environment = _LocalSandboxedEnvironment(undefined=StrictUndefined, autoescape=False, optimized=False)
        template = environment.from_string(source)
        pieces: list[str] = []
        size = 0
        for piece in template.generate(**context):
            size += len(piece.encode("utf-8"))
            if size > _MAX_RENDER_BYTES:
                raise TemplateError(f"Rendered template exceeds {_MAX_RENDER_BYTES} UTF-8 bytes")
            pieces.append(piece)
        connection.send(("ok", "".join(pieces)))
    except (
        TemplateError,
        TemplateSyntaxError,
        TemplateRuntimeError,
        UndefinedError,
        SecurityError,
        MemoryError,
        ArithmeticError,
        TypeError,
        ValueError,
    ) as exc:
        # Keep the protocol bounded and do not pickle a third-party exception.
        connection.send((type(exc).__name__, str(exc)[:1024]))
        raise SystemExit(1) from exc
    finally:
        connection.close()


def _run_template_worker(source: str, payload: bytes) -> str:
    _check_template_source(source)
    if len(payload) > _MAX_CONTEXT_BYTES:
        raise TemplateError(f"Template context exceeds {_MAX_CONTEXT_BYTES} bytes")
    if not _WORKER_SLOTS.acquire(timeout=_WORKER_TIMEOUT_SECONDS):
        raise TemplateError("Too many concurrent template workers")
    process_context = multiprocessing.get_context("spawn")
    parent, child = process_context.Pipe(duplex=False)
    process: Any = None
    try:
        process = cast("Any", process_context).Process(target=_template_worker, args=(child, source, payload))
        process.start()
        child.close()
        if not parent.poll(_WORKER_TIMEOUT_SECONDS):
            raise TemplateError("Template exceeded the execution time limit")
        try:
            status, value = parent.recv()
        except EOFError as exc:
            raise TemplateError("Template worker stopped before completing") from exc
        if status == "ok":
            if type(value) is not str:
                raise TemplateError("Template worker returned a non-string result")
            return value
        if status == "TemplateSyntaxError":
            raise TemplateSyntaxError(value, 1)
        if status == "UndefinedError":
            raise UndefinedError(value)
        if status == "SecurityError":
            raise SecurityError(value)
        if status == "TemplateError":
            raise TemplateError(value)
        if status == "MemoryError":
            raise TemplateError("Template worker exceeded the memory limit")
        raise TemplateRuntimeError(value)
    finally:
        parent.close()
        child.close()
        if process is not None and process.is_alive():
            process.kill()
        if process is not None and process.pid is not None:
            process.join()
        _WORKER_SLOTS.release()


class _BoundedTemplate:
    def __init__(self, source: str) -> None:
        self._source = source

    def render(self, **context: Any) -> str:
        transport = _pack_context_value(context)
        return _run_template_worker(self._source, pickle.dumps(transport, protocol=5))


class _BoundedEnvironment(_LocalSandboxedEnvironment):
    def parse(self, source: str, name: str | None = None, filename: str | None = None) -> nodes.Template:
        _check_template_source(source)
        return super().parse(source, name=name, filename=filename)

    def from_string(
        self,
        source: str | nodes.Template,
        globals: object = None,
        template_class: type[Template] | None = None,
    ) -> Template:
        if globals is not None or template_class is not None:
            raise TemplateError("Custom template globals and classes are unsupported")
        if type(source) is not str:
            raise TemplateError("Pre-parsed Jinja templates are unsupported")
        _check_template_source(source)
        ast = super().parse(source)
        if sum(1 for _ in ast.find_all(nodes.Node)) > 2048:
            raise TemplateError("Template AST exceeds 2048 nodes")
        if next(ast.find_all(nodes.Pow), None) is not None:
            raise TemplateError("Power expressions are not supported in pipeline templates")
        # Jinja's stock compiler folds authored constants here. This
        # environment disables that path, so syntax validation is bounded by
        # the source/AST limits and does not wait for a child on the web loop.
        compiled = super().from_string(ast)
        # Static text has a fixed output no larger than its bounded source.
        # Keep it in-process so ordinary blob paths need no worker startup.
        if all(type(node) is nodes.Output and all(type(child) is nodes.TemplateData for child in node.nodes) for node in ast.body):
            return compiled
        return cast("Template", _BoundedTemplate(source))


def create_sandboxed_environment() -> ImmutableSandboxedEnvironment:
    """Create an ImmutableSandboxedEnvironment with StrictUndefined.

    Returns:
        A sandboxed Jinja2 environment that:
        - Raises on undefined variables (StrictUndefined)
        - Blocks attribute access and method calls (ImmutableSandboxedEnvironment)
        - Does not HTML-escape output (autoescape=False)
        - Bounds compile input, render time, worker memory, and output size
    """
    return _BoundedEnvironment(
        undefined=StrictUndefined,
        autoescape=False,
        optimized=False,
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
