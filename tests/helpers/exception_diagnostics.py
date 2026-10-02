"""Bounded constructor roster, reconciled with executable diagnostic obligations.

Only supplied modules and top-level, locally declared exception classes are
covered. Direct constructor stores are syntax obligations, including branches;
annotations without values and unexecuted nested bodies are not stores. Base
fields belong to their declaring class, and imported base fields are outside
scope. This finite AST pass does not prove arbitrary Python effects or consumer
semantics. Unknown constructor delegation fails for explicit review; consumer
cases separately prove the chosen message, audit, privacy or control contract.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, is_dataclass
from types import FunctionType, ModuleType
from typing import Literal

from elspeth.contracts.audit_evidence import AuditEvidenceBase


@dataclass(frozen=True)
class ExceptionShape:
    exception: type[BaseException]
    fields: frozenset[str]


@dataclass(frozen=True)
class DiagnosticCase:
    exception: type[BaseException]
    fields: frozenset[str]
    channel: Literal["message", "structured", "operator", "control", "private", "inherited"]
    explanation: str
    exercise: Callable[[], None]


class _AuditWrapperControl(AuditEvidenceBase, Exception):
    """Obtain the real nominal wrapper's code identity without reflection probes."""

    def to_audit_dict(self) -> Mapping[str, str]:
        return {}


def _label(exception: type[BaseException]) -> str:
    return f"{exception.__module__}.{exception.__qualname__}"


def check_case_partition(shapes: tuple[ExceptionShape, ...], cases: tuple[DiagnosticCase, ...]) -> None:
    """Require exact class coverage and disjoint, exhaustive owned-field claims."""
    assert shapes, "empty exception shape roster"
    owned = {shape.exception: shape.fields for shape in shapes}
    assert len(owned) == len(shapes), "duplicate exception shapes"
    seen: dict[type[BaseException], set[str]] = {}
    problems: list[str] = []
    for case in cases:
        label = _label(case.exception)
        if case.exception not in owned:
            problems.append(f"stale class {label}")
            continue
        if not case.explanation.strip():
            problems.append(f"empty explanation for {label}")
        if case.channel not in {"message", "structured", "operator", "control", "private", "inherited"}:
            problems.append(f"invalid channel for {label}: {case.channel}")
        if not callable(case.exercise):
            problems.append(f"non-executable case for {label}")
        if case.channel == "inherited" and (case.fields or owned[case.exception]):
            problems.append(f"inherited disposition claims owned fields for {label}")
        prior = seen.setdefault(case.exception, set())
        duplicate = prior & case.fields
        stale = case.fields - owned[case.exception]
        if duplicate:
            problems.append(f"duplicate fields for {label}: {sorted(duplicate)}")
        if stale:
            problems.append(f"stale fields for {label}: {sorted(stale)}")
        prior.update(case.fields)
    for exception, fields in owned.items():
        if exception not in seen:
            problems.append(f"missing class {_label(exception)}")
        missing = fields - seen.get(exception, set())
        if missing:
            problems.append(f"missing fields for {_label(exception)}: {sorted(missing)}")
    assert not problems, "\n".join(problems)


class _ConstructorStores(ast.NodeVisitor):
    """Single finite traversal with a parent stack; no alias/dataflow solver."""

    def __init__(self, receiver: str, label: str, bases: frozenset[str]) -> None:
        self.receiver = receiver
        self.label = label
        self.bases = bases
        self.fields: set[str] = set()
        self.parents: list[ast.AST] = []
        self.nested: set[str] = set()

    def reject(self, node: ast.AST, reason: str) -> None:
        raise AssertionError(f"unsupported constructor ownership for {self.label}: {reason}: {ast.dump(node)}")

    def visit(self, node: ast.AST) -> None:
        self.parents.append(node)
        super().visit(node)
        self.parents.pop()

    def is_receiver(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == self.receiver

    def visit_Name(self, node: ast.Name) -> None:
        if not self.is_receiver(node):
            return
        parent = self.parents[-2]
        if isinstance(parent, ast.Attribute) and parent.value is node:
            return
        if isinstance(parent, ast.Call) and self._parent_init(parent) and parent.args and parent.args[0] is node:
            return
        self.reject(node, "receiver alias, escape or rebinding")

    def _parent_init(self, node: ast.Call) -> bool:
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "__init__"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.bases
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self.is_receiver(node.value):
            if node.attr == "__dict__":
                self.reject(node, "dynamic instance dictionary")
            if isinstance(node.ctx, ast.Del) or isinstance(self.parents[-2], ast.AugAssign):
                self.reject(node, "delete or augmented field mutation")
            if isinstance(node.ctx, ast.Store):
                self.fields.add(node.attr)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.target)
            self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in self.nested:
            self.reject(node, "invoked nested constructor helper")
        if isinstance(node.func, ast.Attribute) and self.is_receiver(node.func.value) and node.func.attr != "_format_message":
            self.reject(node, "receiver method delegation")
        if isinstance(node.func, ast.Lambda):
            self.reject(node, "invoked lambda")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.nested.add(node.name)
        for expression in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults):
            if expression is not None:
                self.visit(expression)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.reject(node, "nested async constructor helper")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.reject(node, "nested class creation")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in (*node.args.defaults, *node.args.kw_defaults):
            if expression is not None:
                self.visit(expression)


def discover_exception_shapes(modules: tuple[ModuleType, ...]) -> tuple[ExceptionShape, ...]:
    """Discover nominal classes and direct fields, rejecting unsupported ownership."""
    shapes: dict[type[BaseException], ExceptionShape] = {}
    wrapper = vars(_AuditWrapperControl)["__init__"]
    assert isinstance(wrapper, FunctionType)
    for module in modules:
        tree = ast.parse(inspect.getsource(module))
        declarations = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        for candidate in vars(module).values():
            if not isinstance(candidate, type) or not issubclass(candidate, BaseException) or candidate.__module__ != module.__name__:
                continue
            if candidate in shapes:
                continue
            label = _label(candidate)
            assert not is_dataclass(candidate), f"unsupported dataclass constructor fields for {label}"
            assert candidate.__qualname__ in declarations, f"unsupported exception declaration for {label}"
            assert "__new__" not in vars(candidate), f"unsupported allocation constructor for {label}"
            declaration = declarations[candidate.__qualname__]
            constructors = [
                node for node in declaration.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__"
            ]
            assert len(constructors) <= 1, f"unsupported duplicate constructor for {label}"
            runtime = vars(candidate).get("__init__")
            recognized_wrapper = (
                issubclass(candidate, AuditEvidenceBase) and isinstance(runtime, FunctionType) and runtime.__code__ is wrapper.__code__
            )
            if not constructors:
                assert runtime is None or recognized_wrapper, f"unsupported generated constructor for {label}"
                fields = frozenset[str]()
            else:
                constructor = constructors[0]
                assert isinstance(constructor, ast.FunctionDef) and not constructor.decorator_list, (
                    f"unsupported decorated/async constructor for {label}"
                )
                assert recognized_wrapper or (
                    isinstance(runtime, FunctionType)
                    and runtime.__code__.co_firstlineno == constructor.lineno
                    and runtime.__code__.co_filename == inspect.getsourcefile(module)
                    and runtime.__qualname__ == f"{candidate.__qualname__}.__init__"
                ), f"unsupported runtime/source constructor disagreement for {label}"
                args = (*constructor.args.posonlyargs, *constructor.args.args)
                assert args, f"unsupported missing constructor receiver for {label}"
                bases = frozenset(base.id for base in declaration.bases if isinstance(base, ast.Name))
                visitor = _ConstructorStores(args[0].arg, label, bases)
                for statement in constructor.body:
                    visitor.visit(statement)
                fields = frozenset(visitor.fields)
            shapes[candidate] = ExceptionShape(candidate, fields)
    assert shapes, "empty exception discovery roster"
    return tuple(sorted(shapes.values(), key=lambda shape: _label(shape.exception)))
