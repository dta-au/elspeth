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
import queue
import resource
import sys
import threading
from atexit import register as register_exit
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from jinja2 import StrictUndefined, Template, TemplateSyntaxError, nodes
from jinja2.compiler import CodeGenerator
from jinja2.exceptions import SecurityError, TemplateRuntimeError, UndefinedError
from jinja2.meta import TrackingCodeGenerator
from jinja2.sandbox import ImmutableSandboxedEnvironment
from jinja2.utils import missing, object_type_repr
from jinja2.visitor import NodeVisitor

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.core.templates import validate_jinja_source


class TemplateError(Exception):
    """Error in template rendering (including sandbox violations)."""


_MAX_RENDER_BYTES = 4 * 1024 * 1024
_MAX_CONTEXT_BYTES = 8 * 1024 * 1024
_MAX_PARENT_PACK_BYTES = 32 * 1024 * 1024
_MAX_CONTEXT_NODES = 65536
# RLIMIT_AS is virtual address space, including the interpreter's existing
# mappings. Cap growth from the spawned worker's own baseline, not an absolute
# address size that depends on which web/test modules Python imported.
_MAX_WORKER_ADDRESS_GROWTH = 256 * 1024 * 1024
_WORKER_TIMEOUT_SECONDS = 5.0
# Two reusable workers bound CPU and memory use. An ordinary row waits for a
# slot instead of becoming a template error when the host is busy.
_WORKER_SLOTS = threading.BoundedSemaphore(2)
_AVAILABLE_WORKERS: queue.SimpleQueue[int] = queue.SimpleQueue()
for _worker_index in range(2):
    _AVAILABLE_WORKERS.put(_worker_index)
_WORKERS: list[tuple[multiprocessing.Process, Any] | None] = [None, None]


def _charge_row_export(value: Any, budget: list[int], *, depth: int = 0) -> None:
    """Bound expanded row work before PipelineRow.to_dict makes a deep copy."""
    if depth > 64:
        raise TemplateError("Template context nesting exceeds 64 levels")
    budget[0] += 1
    budget[1] += sys.getsizeof(value)
    if budget[0] > _MAX_CONTEXT_NODES or budget[1] > _MAX_PARENT_PACK_BYTES:
        raise TemplateError("Template context exceeds the parent packing limit")
    if isinstance(value, (dict, MappingProxyType)):
        for key, item in value.items():
            _charge_row_export(key, budget, depth=depth + 1)
            _charge_row_export(item, budget, depth=depth + 1)
    elif isinstance(value, (list, tuple, frozenset)):
        for item in value:
            _charge_row_export(item, budget, depth=depth + 1)


class _NoFoldCodeGenerator(CodeGenerator):
    """Never evaluate an authored expression while compiling a template."""

    def _output_child_to_const(self, node: nodes.Expr, frame: Any, finalize: Any) -> str:
        if type(node) is nodes.TemplateData:
            return super()._output_child_to_const(node, frame, finalize)
        raise nodes.Impossible()

    def visit_EvalContextModifier(self, node: nodes.EvalContextModifier, frame: Any) -> None:
        for keyword in node.options:
            self.writeline(f"context.eval_ctx.{keyword.key} = ")
            self.visit(keyword.value, frame)
            if type(keyword.value) is nodes.Const:
                setattr(frame.eval_ctx, keyword.key, keyword.value.value)
            else:
                frame.eval_ctx.volatile = True


class _NoFoldTrackingCodeGenerator(_NoFoldCodeGenerator, TrackingCodeGenerator):
    """Use Jinja's symbol analysis without its constant-folding compiler."""

    def __init__(self, environment: ImmutableSandboxedEnvironment) -> None:
        super().__init__(environment)
        # TrackingCodeGenerator hard-codes optimized=True even when its
        # environment has optimized=False. Disable that second folding path.
        self.optimizer = None


class _LocalSandboxedEnvironment(ImmutableSandboxedEnvironment):
    code_generator_class = _NoFoldCodeGenerator


@dataclass(frozen=True)
class _RowTransport:
    data: bytes
    contract: bytes


def _pack_context_value(
    value: Any,
    *,
    depth: int = 0,
    memo: dict[int, Any] | None = None,
    active: set[int] | None = None,
    budget: list[int] | None = None,
) -> Any:
    """Detach frozen carriers with alias preservation and a parent work cap."""
    from elspeth.contracts.freeze import FrozenJsonArray
    from elspeth.contracts.schema_contract import PipelineRow

    if depth > 64:
        raise TemplateError("Template context nesting exceeds 64 levels")
    if memo is None:
        memo = {}
    if active is None:
        active = set()
    if budget is None:
        budget = [0, 0]
    identity = id(value)
    if identity in active:
        raise TemplateError("Template context contains a cyclic container")
    if identity in memo:
        return memo[identity]
    budget[0] += 1
    if type(value) is MappingProxyType:
        estimated_bytes = 64 + 72 * len(value)
    elif type(value) in (dict, list, tuple, FrozenJsonArray, str, bytes, int, float, bool):
        estimated_bytes = sys.getsizeof(value)
    else:
        estimated_bytes = 128
    budget[1] += estimated_bytes
    if budget[0] > _MAX_CONTEXT_NODES or budget[1] > _MAX_PARENT_PACK_BYTES:
        raise TemplateError("Template context exceeds the parent packing limit")
    active.add(identity)
    try:
        if type(value) is PipelineRow:
            _charge_row_export(value._data, budget, depth=depth + 1)
            data = pickle.dumps(value.to_dict(), protocol=5)
            contract = pickle.dumps(value.contract.to_checkpoint_format(), protocol=5)
            budget[1] += len(data) + len(contract)
            if budget[1] > _MAX_PARENT_PACK_BYTES:
                raise TemplateError("Template context exceeds the parent packing limit")
            packed: Any = _RowTransport(data, contract)
        elif type(value) in (dict, MappingProxyType):
            packed = {}
            for key, item in value.items():
                _charge_row_export(key, budget, depth=depth + 1)
                if budget[1] > _MAX_CONTEXT_BYTES:
                    raise TemplateError("Template context exceeds the parent packing limit")
                packed[key] = _pack_context_value(item, depth=depth + 1, memo=memo, active=active, budget=budget)
        elif type(value) is list:
            packed = [_pack_context_value(item, depth=depth + 1, memo=memo, active=active, budget=budget) for item in value]
        elif type(value) is FrozenJsonArray:
            packed = FrozenJsonArray(_pack_context_value(item, depth=depth + 1, memo=memo, active=active, budget=budget) for item in value)
        elif type(value) is tuple:
            packed = tuple(_pack_context_value(item, depth=depth + 1, memo=memo, active=active, budget=budget) for item in value)
        elif isinstance(value, (tuple, frozenset)):
            # deep_freeze preserves these carriers when children are already
            # frozen; charge their expanded payload before pickle sees them.
            _charge_row_export(value, budget, depth=depth)
            if budget[1] > _MAX_CONTEXT_BYTES:
                raise TemplateError("Template context exceeds the parent packing limit")
            packed = value
        else:
            packed = value
    finally:
        active.remove(identity)
    memo[identity] = packed
    return packed


def _restore_context_value(value: Any, *, memo: dict[int, Any] | None = None) -> Any:
    from elspeth.contracts.freeze import FrozenJsonArray

    if memo is None:
        memo = {}
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if type(value) is _RowTransport:
        from elspeth.contracts.schema_contract import PipelineRow, SchemaContract

        restored: Any = PipelineRow(pickle.loads(value.data), SchemaContract.from_checkpoint(pickle.loads(value.contract)))
    elif type(value) is dict:
        restored = {key: _restore_context_value(item, memo=memo) for key, item in value.items()}
    elif type(value) is list:
        restored = [_restore_context_value(item, memo=memo) for item in value]
    elif type(value) is FrozenJsonArray:
        restored = FrozenJsonArray(_restore_context_value(item, memo=memo) for item in value)
    elif type(value) is tuple:
        restored = tuple(_restore_context_value(item, memo=memo) for item in value)
    else:
        restored = value
    memo[identity] = restored
    return restored


def _check_template_source(source: str) -> None:
    try:
        validate_jinja_source(source)
    except ValueError as exc:
        raise TemplateError(str(exc)) from exc


def _template_worker(connection: Any) -> None:
    """Serve bounded renders until the parent closes the pipe or retires us."""
    try:
        baseline_pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[0])
        max_address_space = baseline_pages * os.sysconf("SC_PAGE_SIZE") + _MAX_WORKER_ADDRESS_GROWTH
        resource.setrlimit(resource.RLIMIT_AS, (max_address_space, max_address_space))
        while True:
            try:
                source, payload, value_free = connection.recv()
            except EOFError:
                raise SystemExit(0) from None
            # RLIMIT_CPU is cumulative over a process lifetime. Give each
            # request two more CPU seconds, keeping the inherited hard bound.
            _, hard_limit = resource.getrlimit(resource.RLIMIT_CPU)
            usage = resource.getrusage(resource.RUSAGE_SELF)
            cpu_limit = math.ceil(usage.ru_utime + usage.ru_stime + 2)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, hard_limit))
            connection.send(_render_in_worker(source, payload, value_free))
    finally:
        connection.close()


def _render_in_worker(source: str, payload: bytes, value_free: bool) -> tuple[str, str]:
    try:
        context = pickle.loads(payload)
        if type(context) is not dict or any(type(key) is not str for key in context):
            raise TemplateError("Template worker received an invalid context")
        context = _restore_context_value(context)
        undefined = StrictUndefined
        if value_free:
            parser = _LocalSandboxedEnvironment(undefined=StrictUndefined, autoescape=False, optimized=False)
            undefined = _value_free_undefined(_template_literals(parser.parse(source)))
        environment = _LocalSandboxedEnvironment(undefined=undefined, autoescape=False, optimized=False)
        template = environment.from_string(source)
        pieces: list[str] = []
        size = 0
        for piece in template.generate(**context):
            size += len(piece.encode("utf-8"))
            if size > _MAX_RENDER_BYTES:
                raise TemplateError(f"Rendered template exceeds {_MAX_RENDER_BYTES} UTF-8 bytes")
            pieces.append(piece)
        return "ok", "".join(pieces)
    except _ValueFreeUndefinedError as exc:
        return "safe_undefined", str(exc)[:1024]
    except _ValueFreeUnsafeAccessError as exc:
        return "safe_security", str(exc)[:1024]
    except _UndefinedContractError as exc:
        return "undefined_contract", str(exc)[:1024]
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
        return type(exc).__name__, type(exc).__name__ if value_free else str(exc)[:1024]


def _retire_worker(index: int) -> None:
    entry = _WORKERS[index]
    _WORKERS[index] = None
    if entry is None:
        return
    process, connection = entry
    connection.close()
    if process.is_alive():
        process.kill()
    if process.pid is not None:
        process.join()


def _stop_template_workers() -> None:
    for index in range(len(_WORKERS)):
        _retire_worker(index)


register_exit(_stop_template_workers)


def _run_template_worker(source: str, payload: bytes, *, value_free: bool = False) -> str:
    if len(payload) > _MAX_CONTEXT_BYTES:
        raise TemplateError(f"Template context exceeds {_MAX_CONTEXT_BYTES} bytes")
    index = _AVAILABLE_WORKERS.get_nowait()
    try:
        entry = _WORKERS[index]
        if entry is None or not entry[0].is_alive():
            _retire_worker(index)
            process_context = multiprocessing.get_context("spawn")
            parent, child = process_context.Pipe(duplex=True)
            process = cast("Any", process_context).Process(target=_template_worker, args=(child,))
            process.daemon = True
            try:
                process.start()
            except BaseException:
                parent.close()
                child.close()
                raise
            child.close()
            _WORKERS[index] = (process, parent)
            entry = (process, parent)
        process, parent = entry
        try:
            parent.send((source, payload, value_free))
            if not parent.poll(_WORKER_TIMEOUT_SECONDS):
                _retire_worker(index)
                raise TemplateError("Template exceeded the execution time limit")
            status, value = parent.recv()
        except (EOFError, BrokenPipeError) as exc:
            _retire_worker(index)
            raise TemplateError("Template worker stopped before completing") from exc
        if status == "ok":
            if type(value) is not str:
                raise TemplateError("Template worker returned a non-string result")
            return value
        if value_free:
            if status == "safe_undefined":
                raise TemplateError(f"Undefined variable: {value}")
            if status == "safe_security":
                raise TemplateError(f"Sandbox violation: {value}")
            if status == "undefined_contract":
                raise _UndefinedContractError(value)
            if status == "UndefinedError":
                raise TemplateError(f"Undefined variable: {value} (message withheld: it can quote row data)")
            if status == "SecurityError":
                raise TemplateError(f"Sandbox violation: {value} (message withheld: it can quote row data)")
            if status == "TemplateError":
                raise TemplateError("Template rendering failed: TemplateError (message withheld: it can quote row data)")
            if status == "MemoryError":
                _retire_worker(index)
                raise TemplateError("Template worker exceeded the memory limit")
            raise TemplateError(f"Template rendering failed: {value} (message withheld: it can quote row data)")
        if status == "TemplateSyntaxError":
            raise TemplateSyntaxError(value, 1)
        if status == "UndefinedError":
            raise UndefinedError(value)
        if status == "SecurityError":
            raise SecurityError(value)
        if status == "TemplateError":
            raise TemplateError(value)
        if status == "MemoryError":
            _retire_worker(index)
            raise TemplateError("Template worker exceeded the memory limit")
        raise TemplateRuntimeError(value)
    finally:
        _AVAILABLE_WORKERS.put(index)


class _BoundedTemplate:
    def __init__(self, source: str, *, value_free: bool = False) -> None:
        self._source = source
        self._value_free = value_free

    def render(self, **context: Any) -> str:
        _check_template_source(self._source)
        _WORKER_SLOTS.acquire()
        try:
            transport = _pack_context_value(context)
            return _run_template_worker(self._source, pickle.dumps(transport, protocol=5), value_free=self._value_free)
        finally:
            _WORKER_SLOTS.release()


class _BoundedEnvironment(_LocalSandboxedEnvironment):
    def __init__(self, *, value_free: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._value_free = value_free

    def parse(self, source: str, name: str | None = None, filename: str | None = None) -> nodes.Template:
        _check_template_source(source)
        try:
            ast = super().parse(source, name=name, filename=filename)
        except RecursionError as exc:
            raise TemplateError("Template expression nesting exceeds the parser limit") from exc
        _check_template_ast(ast)
        return ast

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
        ast = self.parse(source)
        # Jinja's stock compiler folds authored constants here. This
        # environment disables that path, so syntax validation is bounded by
        # the source/AST limits and does not wait for a child on the web loop.
        compiled = super().from_string(ast)
        # Static text has a fixed output no larger than its bounded source.
        # Keep it in-process so ordinary blob paths need no worker startup.
        if all(type(node) is nodes.Output and all(type(child) is nodes.TemplateData for child in node.nodes) for node in ast.body):
            return compiled
        return cast("Template", _BoundedTemplate(source, value_free=self._value_free))


def _check_template_ast(ast: nodes.Template) -> None:
    # Admit depth before Jinja's code generator and our name analysis recurse.
    # find_all itself recurses, so it cannot safely enforce this budget.
    pending = [(child, 1) for child in ast.iter_child_nodes()]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 2048:
            raise TemplateError("Template AST exceeds 2048 nodes")
        if depth > 64:
            raise TemplateError("Template AST nesting exceeds 64 levels")
        pending.extend((child, depth + 1) for child in node.iter_child_nodes())
    if next(ast.find_all(nodes.Pow), None) is not None:
        raise TemplateError("Power expressions are not supported in pipeline templates")
    for modifier in ast.find_all(nodes.EvalContextModifier):
        if any(type(option.value) is not nodes.Const or type(option.value.value) is not bool for option in modifier.options):
            raise TemplateError("Template autoescape requires a literal boolean")


def create_sandboxed_environment(*, value_free: bool = False) -> ImmutableSandboxedEnvironment:
    """Create an ImmutableSandboxedEnvironment with StrictUndefined.

    Args:
        value_free: Render failures use only operator-authored keys and types.

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
        value_free=value_free,
    )


# Exceptions a render of an operator's template over row data may raise as a
# per-row operational failure (tier-model-deep-dive: "Pipeline Templates as
# Tier 2 Data"). UndefinedError and SecurityError are TemplateRuntimeErrors;
# OverflowError and ZeroDivisionError are ArithmeticErrors.
_RENDER_FAILURES = (TemplateSyntaxError, TemplateRuntimeError, ArithmeticError, TypeError, ValueError)

# Stands in for a lookup key the template does not spell out, i.e. one computed
# at render time (``row[row.k]``, ``attr(row.k)``, ``row.items[row.i]``).
_UNSPELLED_KEY = "<a key the template does not spell out>"


class _ValueFreeUndefinedError(UndefinedError):
    """An undefined lookup whose message ELSPETH built, naming no row value."""


class _ValueFreeUnsafeAccessError(SecurityError):
    """A sandbox-refused attribute lookup whose message ELSPETH built, naming no row value."""


class _UndefinedContractError(RuntimeError):
    """The installed Jinja2 no longer uses its documented undefined contract."""


def withheld_error_detail(exc: BaseException) -> str:
    """The value-free text of a failure whose own message may quote row data: its class only.

    Jinja's messages are not value-free. An undefined or sandbox-refused
    lookup quotes its KEY, and a template may compute that key from the row
    (``{{ row[row.k] }}`` renders ``'dict object' has no attribute '<the value
    of k>'``). A Python error inside the template quotes operands
    (``wordwrap`` renders ``invalid width -5``, a codec error quotes the
    offending character and its offset), and the canonicalizer quotes an
    out-of-range integer. The row itself stays attributable through the token
    (``transform_errors`` stores it), so the failing input can be recovered
    without copying it into the reason.
    """
    return f"{type(exc).__name__} (message withheld: it can quote row data)"


def _template_literals(ast: nodes.Template) -> frozenset[str | int]:
    """Every name and constant the operator wrote into the template.

    A lookup key found here is printable: it is config text, not row text.
    A row value that happens to equal one of these prints as that config text,
    the same rule as ``safe_validation_error_text`` printing a field its schema
    declares.
    """
    literals: set[str | int] = set()
    for name_node in ast.find_all(nodes.Name):
        literals.add(name_node.name)
    for getattr_node in ast.find_all(nodes.Getattr):
        literals.add(getattr_node.attr)
    for keyword in ast.find_all(nodes.Keyword):
        literals.add(keyword.key)
    for const in ast.find_all(nodes.Const):
        value = const.value
        if type(value) is str:
            # A dotted literal (``map(attribute='a.b')``) is looked up part by part.
            literals.add(value)
            literals.update(value.split("."))
        elif type(value) is int:
            # ``items[-1]`` parses as Neg(Const(1)).
            literals.update((value, -value))
    return frozenset(literals)


def _value_free_undefined(literals: frozenset[str | int]) -> type[StrictUndefined]:
    """A StrictUndefined whose error message names only what the template spells out.

    Every failing operation on an Undefined (``__str__``, ``__add__``,
    ``__getattr__`` ...) raises ``self._undefined_exception(self._undefined_message)``,
    so these two hooks cover them all. Jinja's hint text is never used: the
    sandbox's unsafe-attribute hint quotes the (possibly row-derived) key.
    """

    class _ValueFreeUndefined(StrictUndefined):
        __slots__ = ()

        def __init__(
            self,
            hint: str | None = None,
            obj: Any = missing,
            name: str | None = None,
            exc: type[TemplateRuntimeError] = UndefinedError,
        ) -> None:
            if exc is UndefinedError:
                value_free_exc: type[TemplateRuntimeError] = _ValueFreeUndefinedError
            elif exc is SecurityError:
                value_free_exc = _ValueFreeUnsafeAccessError
            else:
                raise _UndefinedContractError(f"jinja2 built an Undefined with an unexpected exception type {exc.__name__}")
            super().__init__(hint, obj, name, value_free_exc)

        @property
        def _undefined_message(self) -> str:
            name: object = self._undefined_name
            if (type(name) is str or type(name) is int) and name in literals:
                key = repr(name)
            else:
                key = _UNSPELLED_KEY
            if self._undefined_exception is _ValueFreeUnsafeAccessError:
                return f"access to attribute {key} of {object_type_repr(self._undefined_obj)} is unsafe"
            if self._undefined_obj is missing:
                return "a value is undefined" if name is None else f"{key} is undefined"
            if type(name) is str:
                return f"{object_type_repr(self._undefined_obj)!r} has no attribute {key}"
            return f"{object_type_repr(self._undefined_obj)} has no element {key}"

    return _ValueFreeUndefined


class SandboxedTemplate:
    """An operator-authored template validated before bounded, value-free rendering.

    Rendering is where a template meets row data, so a render failure raises
    ``TemplateError`` whose message names no row value (see ``render``). Each caller keeps its own config-time handling of
    ``TemplateSyntaxError`` from the constructor.
    """

    __slots__ = ("_template",)

    def __init__(self, source: str) -> None:
        """Validate and compile ``source`` without evaluating row expressions.

        Raises:
            TemplateSyntaxError: The template is malformed (including an
                unknown filter or test, a ``TemplateAssertionError``).
        """
        self._template = create_sandboxed_environment(value_free=True).from_string(source)

    def render(self, **context: Any) -> str:
        """Render with ``context``: the ONE place a render failure becomes text.

        Only two texts are ever emitted: a lookup failure raised by this
        template's own undefined type, whose message names the owner's TYPE and
        the key only when the operator's template spells that key out; and, for
        anything else, the exception's class name (``withheld_error_detail``).

        Raises:
            TemplateError: A per-row operational failure, prefixed by its kind
                (``Undefined variable``, ``Sandbox violation``, ``Template
                rendering failed``) with a value-free detail.
        """
        try:
            return self._template.render(**context)
        except _ValueFreeUndefinedError as exc:
            raise TemplateError(f"Undefined variable: {exc}") from exc
        except _ValueFreeUnsafeAccessError as exc:
            raise TemplateError(f"Sandbox violation: {exc}") from exc
        except UndefinedError as exc:
            raise TemplateError(f"Undefined variable: {withheld_error_detail(exc)}") from exc
        except SecurityError as exc:
            raise TemplateError(f"Sandbox violation: {withheld_error_detail(exc)}") from exc
        except _RENDER_FAILURES as exc:
            raise TemplateError(f"Template rendering failed: {withheld_error_detail(exc)}") from exc


def find_runtime_unbound_variables(ast: nodes.Template) -> frozenset[str]:
    """Return names that may require render context on a reachable path.

    Jinja's ``find_undeclared_variables`` deliberately reports names assigned
    in conditional branches because its code-generation analysis merges all
    branch stores. That is too broad for ELSPETH's StrictUndefined preflight:
    a local assigned in every branch is defined when a later interpolation
    runs. Keep Jinja's conservative candidate set, then remove a candidate only
    when a path- and order-sensitive walk proves it bound at every load.
    """
    if type(ast.environment) is not _BoundedEnvironment:
        raise TemplateError("Template name discovery requires a bounded environment")
    _check_template_ast(ast)
    tracker = _NoFoldTrackingCodeGenerator(ast.environment)
    tracker.visit(ast)
    candidates = frozenset(tracker.undeclared_identifiers)
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
