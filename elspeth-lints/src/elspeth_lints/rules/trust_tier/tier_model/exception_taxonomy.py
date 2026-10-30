"""Finite, source-only nominal exception facts for R5's checked operand.

This classifies declared exception *kinds*, not the subject's runtime type or
a handler's disposition. Supported binding provenance and explicit modeled
namespace effects constrain the facts. Declared instance forwarding is a
source role, not a proof of runtime receiver type, MRO, isolated storage or
callback purity. Unmodeled transitive effects are outside this rule's claim.
Ordinary object element writes do not establish module binding effects;
explicit namespace origins and actual contained-object member writes remain vetoes.
An unproved read result is unknown, not a recovered namespace or a purity fact.
Read closure below covers finite declared-source facts, not runtime read closure.
Unknown checked bindings/ancestry remain unknown. The index owns only
the original ASTs explicitly supplied by one scan; it never imports code,
discovers dependencies, evaluates decorators, or caches facts across scans.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from elspeth_lints.core.boundary_aliases import argument_names, import_alias_effect, match_pattern_binding_names

# Deterministic Python 3.12+ API facts, including the common intermediate
# bases of owned exceptions. This deliberately models neither every builtin
# nor arbitrary third-party exports. Aliases need the same lexical proof.
_BUILTIN_BASES: dict[str, tuple[str, ...]] = {
    "BaseException": (),
    "Exception": ("BaseException",),
    "ArithmeticError": ("Exception",),
    "AssertionError": ("Exception",),
    "AttributeError": ("Exception",),
    "EOFError": ("Exception",),
    "ImportError": ("Exception",),
    "LookupError": ("Exception",),
    "MemoryError": ("Exception",),
    "NameError": ("Exception",),
    "OSError": ("Exception",),
    "ReferenceError": ("Exception",),
    "RuntimeError": ("Exception",),
    "StopAsyncIteration": ("Exception",),
    "StopIteration": ("Exception",),
    "SyntaxError": ("Exception",),
    "SystemError": ("Exception",),
    "TypeError": ("Exception",),
    "ValueError": ("Exception",),
    "Warning": ("Exception",),
    "IndexError": ("LookupError",),
    "KeyError": ("LookupError",),
    "ModuleNotFoundError": ("ImportError",),
    "NotImplementedError": ("RuntimeError",),
    "RecursionError": ("RuntimeError",),
    "UnboundLocalError": ("NameError",),
    "UnicodeError": ("ValueError",),
}
_EXTERNAL_BASES: dict[str, tuple[str, ...]] = {
    **{f"builtins.{name}": tuple(f"builtins.{base}" for base in bases) for name, bases in _BUILTIN_BASES.items()},
    "sqlalchemy.exc.SQLAlchemyError": ("builtins.Exception",),
    # A transitive API ancestry fact; no other sqlalchemy export is inferred.
    "sqlalchemy.exc.IntegrityError": ("sqlalchemy.exc.SQLAlchemyError",),
}
_CLASS_HOOKS = frozenset({"__init_subclass__", "__instancecheck__", "__subclasscheck__", "__mro_entries__"})
_NAMESPACE_FUNCTIONS = frozenset({"globals", "locals", "vars", "setattr", "delattr", "getattr", "exec", "eval"})
_NAMESPACE_ATTRIBUTES = frozenset({"__dict__", "__globals__", "__builtins__"})
_REFLECTION_GETTERS = frozenset({"builtins.getattr", "inspect.getattr_static"})
_REFLECTION_FACTORIES = frozenset({"operator.attrgetter"})
_REFLECTION_CALLABLE_NAMES = frozenset(name.rsplit(".", 1)[-1] for name in _REFLECTION_GETTERS | _REFLECTION_FACTORIES)
_NAMESPACE_MUTATORS = frozenset({"__setattr__", "__delattr__"})
_DESCRIPTOR_MUTATORS = frozenset({"__set__", "__delete__"})
# These mapping-method capabilities are effects only on a veto-origin
# namespace receiver; ordinary instance methods with these names stay opaque.
_NAMESPACE_METHOD_MUTATORS = frozenset(
    {"update", "clear", "pop", "popitem", "setdefault", "__setitem__", "__delitem__", "__ior__", "__init__"}
)
_SLOT_KINDS: dict[str, Literal["read", "write", "delete"]] = {"__getattribute__": "read", "__setattr__": "write", "__delattr__": "delete"}
_EFFECT_BUILTINS = frozenset({"object", "type", "super", "setattr", "delattr"})
_BUILTIN_EXCEPTION_SLOT_OWNERS = frozenset(f"builtins.{name}" for name in _BUILTIN_BASES)
_UNBOUND_SLOT_OWNERS = frozenset({"builtins.object", "builtins.type"}) | _BUILTIN_EXCEPTION_SLOT_OWNERS
_BUILTIN_DESCRIPTOR_READS = {
    "builtins.type": frozenset({"__name__"}),
    "builtins.BaseException": frozenset({"__traceback__", "__cause__", "__context__", "__suppress_context__"}),
}
_NON_INSTANCE_METHODS = frozenset({"__new__", "__init_subclass__", "__class_getitem__"})
_MAX_VETO_DEPTH = 64
_NAMESPACE_ORIGIN = "@namespace:"
_UNKNOWN_READ_ORIGIN = "@unknown-read:"
_FACTORY_ORIGIN = "@factory:"
_NAMESPACE_CONSTRUCTORS = frozenset({"globals", "locals", "vars"})
_READ_CALLABLE_NAMES = _REFLECTION_CALLABLE_NAMES | {"__getattribute__"} | _NAMESPACE_CONSTRUCTORS
_EFFECT_CALLABLE_NAMES = _NAMESPACE_MUTATORS | _DESCRIPTOR_MUTATORS | {"setattr", "delattr", "exec", "eval"}
_NAMESPACE_READ_METHODS = (
    frozenset({"get", "keys", "values", "items", "copy", "__getitem__", "__contains__", "__iter__", "__len__"}) | _READ_CALLABLE_NAMES
)
_ScopeNode = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda


def _namespace_origin(reference: str | _ClassRef) -> bool:
    return isinstance(reference, str) and (
        reference.startswith(_NAMESPACE_ORIGIN)
        or reference.endswith(".__getattribute__")
        or bool(_NAMESPACE_ATTRIBUTES.intersection(reference.split(".")))
    )


@dataclass(frozen=True)
class _ClassRef:
    file_path: str
    node_id: int


@dataclass(frozen=True)
class _Binding:
    node: ast.AST
    imported: str | None = None


@dataclass
class _Scope:
    node: _ScopeNode
    parent: _Scope | None = None
    bindings: dict[str, list[_Binding]] = field(default_factory=dict)
    star_import: bool = False
    type_parameters: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _Capability:
    effect: bool
    namespace_mutator: bool
    namespace_read: bool
    reader_kind: str | None
    namespace_path_mutators: tuple[bool, ...]


def _selected_capability(selectors: tuple[str | None, ...], *, dotted: bool = False) -> _Capability:
    """Literal veto facts only; these never resolve a favorable type alias."""
    paths = tuple(tuple(selector.split(".") if dotted else [selector]) if selector is not None else () for selector in selectors)
    parts = tuple(part for path in paths for part in path)
    namespace_path_mutators: list[bool] = []
    for path in paths:
        namespace_seen = False
        selected_mutator = False
        for part in path:
            selected_mutator = selected_mutator or (namespace_seen and part in _NAMESPACE_METHOD_MUTATORS)
            namespace_seen = namespace_seen or part in _NAMESPACE_ATTRIBUTES
        # Each factory selector starts again at the application receiver.
        # Namespace recovery in one tuple selector cannot authorize another.
        namespace_path_mutators.append(selected_mutator)
    leaf = parts[-1] if len(selectors) == 1 and selectors[0] is not None and parts else None
    reader = leaf if leaf in _READ_CALLABLE_NAMES else None
    return _Capability(
        bool(_EFFECT_CALLABLE_NAMES.intersection(parts)),
        bool(_NAMESPACE_METHOD_MUTATORS.intersection(parts)),
        bool(_NAMESPACE_ATTRIBUTES.intersection(parts)) or reader is not None,
        reader,
        tuple(namespace_path_mutators),
    )


@dataclass(frozen=True)
class _SelectedCapability:
    scope: _Scope
    node: ast.expr
    receiver: ast.expr
    capability: _Capability
    receiver_module: _Module
    receiver_scope: _Scope
    namespace_bound: bool = False


@dataclass(frozen=True)
class _FactorySelection:
    module: _Module
    call: ast.Call
    selectors: tuple[str | None, ...]
    supported: bool
    canonical_constructor: bool


@dataclass(frozen=True)
class _Class:
    module: _Module
    node: ast.ClassDef
    qualified_name: str


@dataclass(frozen=True)
class _SlotOperation:
    module: _Module
    scope: _Scope
    call: ast.Call
    receiver: ast.expr
    member: ast.expr
    kind: Literal["read", "write", "delete"]
    dependencies: tuple[str, ...] = ()
    declared_super: bool = False
    namespace_owner: bool = False
    binding_dependencies: tuple[tuple[ast.expr, str], ...] = ()
    exception_receiver_abi: bool = False


@dataclass(frozen=True)
class _BuiltinDescriptorRead:
    module: _Module
    call: ast.Call
    owner: ast.expr
    reference: str
    member: str
    receiver_type: ast.Call | None = None


@dataclass(frozen=True)
class _InstanceDictionaryOperation:
    """Original declared member effect, not a runtime receiver fact."""

    module: _Module
    scope: _Scope
    node: ast.Subscript | ast.Call
    receiver: ast.Name
    member: str
    default: ast.expr | None = None


def _dotted_name(node: ast.expr) -> tuple[str, tuple[str, ...]] | None:
    attributes: list[str] = []
    while isinstance(node, ast.Attribute):
        attributes.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return node.id, tuple(reversed(attributes))


class _Module:
    """Original-AST bindings; source order cannot erase a deferred rebind."""

    def __init__(self, file_path: str, name: str, tree: ast.Module) -> None:
        self.file_path = file_path
        self.name = name
        self.tree = tree
        nodes = tuple(ast.walk(tree))
        self.parents = {id(child): parent for parent in nodes for child in ast.iter_child_nodes(parent)}
        self.scopes = {
            id(node): _Scope(node)
            for node in nodes
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
        }
        self.root = self.scopes[id(tree)]
        for indexed_scope in self.scopes.values():
            if indexed_scope is not self.root:
                indexed_scope.parent = self.scope_of(indexed_scope.node)
        self.declared_mutable: set[str] = set()
        self.opaque_namespace = False
        self.unsupported_namespace = False
        self.unsupported_read_contexts: set[int] = set()
        self.namespace_values: set[int] = set()
        self.unknown_read_values: set[int] = set()
        # Explicit defaults/other argument facts, excluding the read receiver.
        self.read_defaults: dict[int, tuple[ast.expr, ...]] = {}
        self.namespace_nodes: dict[int, ast.expr] = {}
        self.namespace_callable_values: dict[int, frozenset[str]] = {}
        self.factory_values: dict[int, str] = {}
        self.selected_capabilities: dict[tuple[int, int, _Capability, bool], _SelectedCapability] = {}
        self.attribute_writes: list[tuple[_Scope, str, tuple[str, ...]]] = []
        self.expression_writes: list[tuple[_Scope, ast.expr, tuple[str, ...]]] = []
        self.subscript_writes: list[tuple[_Scope, ast.Subscript]] = []
        self.namespace_selections: list[tuple[_Scope, ast.Subscript]] = []
        self.namespace_calls: list[tuple[_Scope, ast.Call]] = []
        self.callable_recoveries: list[tuple[_Scope, ast.Attribute]] = []
        self.namespace_method_recoveries: list[tuple[_Scope, ast.Attribute]] = []
        self.classes: dict[_ClassRef, _Class] = {}
        deferred_global_reads: list[tuple[_Scope, ast.Call]] = []
        for node in nodes:
            scope = self.scope_of(node)
            if scope is None:
                continue
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                self.declared_mutable.update(node.names)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                self._bind(scope, node.id, node)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                effect = import_alias_effect(node)
                scope.star_import = scope.star_import or effect.clears_all
                for local, imported in effect.proven:
                    self._bind(scope, local, node, imported)
                    if imported.endswith((".__setattr__", ".__delattr__")):
                        self.unsupported_namespace = True
                    if imported.endswith(".__getattribute__"):
                        self.unsupported_read_contexts.add(id(scope.node))
                    if imported.rsplit(".", 1)[-1] in _NAMESPACE_ATTRIBUTES:
                        self.unsupported_read_contexts.add(id(scope.node))
                for local in effect.invalidated:
                    self._bind(scope, local, node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._bind(scope, node.name, node)
                own = self.scopes[id(node)]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for local in argument_names(node.args):
                        self._bind(own, local, node.args)
                for parameter in node.type_params:
                    if isinstance(parameter, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)):
                        self._bind(own, parameter.name, parameter)
                        own.type_parameters.add(parameter.name)
                    else:
                        own.star_import = True
                if isinstance(node, ast.ClassDef):
                    prefixes: list[str] = [node.name]
                    current: _Scope | None = scope
                    while current is not None:
                        if isinstance(current.node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            prefixes.append(current.node.name)
                        current = current.parent
                    qualified = ".".join((name, *reversed(prefixes)))
                    self.classes[_ClassRef(file_path, id(node))] = _Class(self, node, qualified)
            elif isinstance(node, ast.Lambda):
                for local in argument_names(node.args):
                    self._bind(self.scopes[id(node)], local, node.args)
            elif isinstance(node, ast.ExceptHandler) and node.name is not None:
                self._bind(scope, node.name, node)
            elif isinstance(node, ast.match_case):
                for local in match_pattern_binding_names(node.pattern):
                    self._bind(scope, local, node.pattern)
            elif isinstance(node, ast.TypeAlias):
                self._bind(scope, node.name.id, node)
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                dotted = _dotted_name(node)
                if dotted is not None:
                    self.attribute_writes.append((scope, *dotted))
                else:
                    self.expression_writes.append((scope, node.value, (node.attr,)))
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                # Element targets are distinct from lexical/member bindings.
                self.subscript_writes.append((scope, node))
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr == "__dict__":
                self.unsupported_namespace = True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"globals", "locals"} or (node.func.id == "vars" and not node.args):
                    parent = self.parents.get(id(node))
                    if (
                        isinstance(parent, ast.Subscript)
                        and isinstance(parent.ctx, (ast.Store, ast.Del))
                        and isinstance(parent.slice, ast.Constant)
                        and isinstance(parent.slice.value, str)
                    ):
                        self.declared_mutable.add(parent.slice.value)
                    if node.func.id == "globals" and isinstance(parent, ast.Subscript) and isinstance(parent.ctx, (ast.Store, ast.Del)):
                        deferred_global_reads.append((scope, node))
                    else:
                        self.record_namespace_read(scope, node)
                elif node.func.id == "vars" and node.args:
                    self.record_namespace_read(scope, node)
            elif isinstance(node, ast.Attribute) and node.attr == "__dict__" and isinstance(node.ctx, ast.Load):
                parent = self.parents.get(id(node))
                # Membership only reads the actual dictionary; it does not
                # hand its namespace to a caller, alias, or mutating method.
                if not (
                    isinstance(parent, ast.Compare)
                    and len(parent.ops) == 1
                    and isinstance(parent.ops[0], (ast.In, ast.NotIn))
                    and parent.comparators[0] is node
                ):
                    self.record_namespace_read(scope, node)
            if isinstance(node, ast.Call):
                self.namespace_calls.append((scope, node))
            if (
                (isinstance(node, ast.Attribute) and node.attr in {"__globals__", "__builtins__"})
                or (isinstance(node, ast.Name) and node.id == "__builtins__" and isinstance(node.ctx, ast.Load))
                or (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)
                    and node.slice.value in {"__globals__", "__builtins__"}
                )
            ):
                if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    self.unsupported_namespace = True
                else:
                    self.record_namespace_read(scope, node)
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                self.namespace_selections.append((scope, node))
            if isinstance(node, ast.Attribute) and node.attr in _SLOT_KINDS:
                parent = self.parents.get(id(node))
                if not (isinstance(parent, ast.Call) and parent.func is node):
                    if isinstance(node.ctx, ast.Load) and self.is_identity_comparison(node):
                        # Identity comparison consumes the reference as data;
                        # even a setter reference is not invoked or escaped.
                        self.unsupported_read_contexts.add(id(scope.node))
                    elif isinstance(node.ctx, ast.Load) and node.attr == "__getattribute__":
                        self.record_namespace_read(scope, node, callable_kind="__getattribute__")
                    else:
                        self.unsupported_namespace = True
            if isinstance(node, ast.Attribute) and node.attr in {"exec", "eval"}:
                self.unsupported_namespace = True
            if isinstance(node, ast.Attribute) and node.attr in _DESCRIPTOR_MUTATORS:
                if self.is_identity_comparison(node):
                    self.unsupported_read_contexts.add(id(scope.node))
                else:
                    self.unsupported_namespace = True
            if isinstance(node, ast.Attribute) and node.attr in _REFLECTION_CALLABLE_NAMES and isinstance(node.ctx, ast.Load):
                self.callable_recoveries.append((scope, node))
            if isinstance(node, ast.Attribute) and node.attr in _NAMESPACE_METHOD_MUTATORS and isinstance(node.ctx, ast.Load):
                self.namespace_method_recoveries.append((scope, node))
        self.own_global_writes: set[int] = set()
        for scope, call in deferred_global_reads:
            if self._is_own_global_write(scope, call):
                self.own_global_writes.add(id(self.parents[id(call)]))
            else:
                self.record_namespace_read(scope, call)

    def _is_own_global_write(self, scope: _Scope, call: ast.Call) -> bool:
        """Only the original unshadowed globals() has an exclusive local owner."""
        return (
            not call.args
            and not call.keywords
            and not self.unsupported_annotation_scope(call)
            and "globals" not in self.declared_mutable
            and all(
                (annotation_only or not candidate.star_import) and not self._visible_bindings(candidate, "globals", annotation_only)
                for candidate, annotation_only in self.lookup_scopes(scope)
            )
        )

    def is_identity_comparison(self, node: ast.expr) -> bool:
        """An immediate identity operand consumes a reference without escape."""
        parent = self.parents.get(id(node))
        return isinstance(parent, ast.Compare) and all(isinstance(op, (ast.Is, ast.IsNot)) for op in parent.ops)

    def record_namespace_read(self, scope: _Scope, node: ast.expr, *, callable_kind: str | None = None) -> None:
        """Explicit namespace/reader recovery withdraws source-context proof.

        The marker is used only by the existing finite mutation-origin walk.
        It is not a positive receiver/type fact or a callback-purity promise.
        A later explicit write/mutator/callable use still withdraws graph facts.
        """
        self.unsupported_read_contexts.add(id(scope.node))
        self.namespace_values.add(id(node))
        self.namespace_nodes[id(node)] = node
        if callable_kind is not None:
            self.namespace_callable_values[id(node)] = self.namespace_callable_values.get(id(node), frozenset()) | {callable_kind}

    def record_unknown_read(
        self,
        scope: _Scope,
        node: ast.expr,
        *,
        default: ast.expr | None = None,
        veto_sources: tuple[ast.expr, ...] = (),
        withdraw_proof: bool = True,
    ) -> None:
        """Unknown read results cannot supply namespace or favorable class facts."""
        self.unknown_read_values.add(id(node))
        sources = (*veto_sources, *((default,) if default is not None else ()))
        if sources:
            self.read_defaults[id(node)] = tuple(dict.fromkeys((*self.read_defaults.get(id(node), ()), *sources)))
        if withdraw_proof:
            self.unsupported_read_contexts.add(id(scope.node))

    def has_unsupported_read_context(self, node: ast.AST) -> bool:
        current: ast.AST | None = node
        while current is not None:
            if id(current) in self.unsupported_read_contexts:
                return True
            current = self.parents.get(id(current))
        return False

    @staticmethod
    def _bind(scope: _Scope, name: str, node: ast.AST, imported: str | None = None) -> None:
        scope.bindings.setdefault(name, []).append(_Binding(node, imported))

    def scope_of(self, node: ast.AST) -> _Scope | None:
        child = node
        while id(child) in self.parents:
            parent = self.parents[id(child)]
            if isinstance(parent, ast.Module):
                return self.scopes[id(parent)]
            if isinstance(parent, ast.Lambda) and child is parent.body:
                return self.scopes[id(parent)]
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and any(child is item for item in parent.body):
                return self.scopes[id(parent)]
            child = parent
        return None

    def unsupported_annotation_scope(self, node: ast.AST) -> bool:
        child = node
        while id(child) in self.parents:
            parent = self.parents[id(child)]
            if isinstance(parent, ast.TypeAlias):
                return True
            if (
                isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and parent.type_params
                and not any(child is item for item in parent.body)
            ):
                return True
            child = parent
        return False

    @staticmethod
    def lookup_scopes(scope: _Scope) -> Iterator[tuple[_Scope, bool]]:
        yield scope, False
        current = scope.parent
        while current is not None:
            annotation_only = isinstance(current.node, ast.ClassDef)
            if not annotation_only or current.type_parameters:
                yield current, annotation_only
            current = current.parent

    @staticmethod
    def _visible_bindings(scope: _Scope, name: str, annotation_only: bool) -> list[_Binding]:
        return scope.bindings.get(name, []) if not annotation_only or name in scope.type_parameters else []

    def lookup(self, scope: _Scope, name: str) -> str | _ClassRef | None:
        if self.opaque_namespace or name in self.declared_mutable:
            return None
        for candidate, annotation_only in self.lookup_scopes(scope):
            if candidate.star_import and not annotation_only:
                return None
            bindings = self._visible_bindings(candidate, name, annotation_only)
            if not bindings:
                continue
            if any(self.parents.get(id(binding.node)) is not candidate.node for binding in bindings):
                return None
            imported = {binding.imported for binding in bindings}
            if None not in imported and len(imported) == 1:
                return bindings[0].imported
            if len(bindings) == 1 and isinstance(bindings[0].node, ast.ClassDef):
                return _ClassRef(self.file_path, id(bindings[0].node))
            return None
        if name in _BUILTIN_BASES or name == "isinstance":
            return f"builtins.{name}"
        return None

    def builtin_fallback(self, node: ast.expr) -> bool:
        scope = self.scope_of(node)
        return (
            isinstance(node, ast.Name)
            and scope is not None
            and node.id not in self.declared_mutable
            and (node.id in _BUILTIN_BASES or node.id == "isinstance")
            and not self.opaque_namespace
            and all(
                (annotation_only or not candidate.star_import) and not self._visible_bindings(candidate, node.id, annotation_only)
                for candidate, annotation_only in self.lookup_scopes(scope)
            )
        )

    def resolve(self, node: ast.expr) -> str | _ClassRef | None:
        dotted = _dotted_name(node)
        scope = self.scope_of(node)
        if dotted is None or scope is None:
            return None
        name, attributes = dotted
        root = self.lookup(scope, name)
        if not attributes:
            return root
        return ".".join((root, *attributes)) if isinstance(root, str) else None

    def effect_reference(self, node: ast.expr) -> str | _ClassRef | None:
        """Positive lexical provenance for finite effects, not exception kinds."""
        if self.unsupported_annotation_scope(node):
            return None
        resolved = self.resolve(node)
        if resolved is not None:
            return resolved
        scope = self.scope_of(node)
        if (
            isinstance(node, ast.Name)
            and node.id in _EFFECT_BUILTINS
            and scope is not None
            and not self.opaque_namespace
            and node.id not in self.declared_mutable
            and all(
                (annotation_only or not candidate.star_import) and not self._visible_bindings(candidate, node.id, annotation_only)
                for candidate, annotation_only in self.lookup_scopes(scope)
            )
        ):
            return f"builtins.{node.id}"
        return None

    def _assigned_value(self, node: ast.AST) -> ast.expr | None:
        current = node
        while id(current) in self.parents:
            parent = self.parents[id(current)]
            if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                return parent.value
            if not isinstance(parent, (ast.Tuple, ast.List, ast.Starred)):
                return None
            current = parent
        return None

    def possible_value_origins(
        self,
        scope: _Scope,
        value: ast.expr,
        seen: frozenset[tuple[int, str]] = frozenset(),
        *,
        member_write: bool = False,
    ) -> tuple[set[str | _ClassRef], bool]:
        """Veto origins; member writes alone retain subscript-container facts."""
        if id(value) in self.factory_values:
            return {self.factory_values[id(value)]}, True
        if id(value) in self.namespace_values or id(value) in self.unknown_read_values:
            kinds = self.namespace_callable_values.get(id(value), frozenset())
            prefix = _NAMESPACE_ORIGIN if id(value) in self.namespace_values else _UNKNOWN_READ_ORIGIN
            roots: set[str | _ClassRef] = {f"{prefix}{id(value)}" + (f".{kind}" if kind else "") for kind in kinds or {""}}
            for default in self.read_defaults.get(id(value), ()):
                possible, _unknown = self.possible_value_origins(scope, default, seen, member_write=member_write)
                roots.update(possible)
            if member_write and isinstance(value, ast.Subscript):
                # Only a later member write consumes this container evidence.
                # Ordinary read results never inherit their receiver origins.
                possible, _unknown = self.possible_value_origins(scope, value.value, seen, member_write=True)
                roots.update(possible)
            return roots, True
        current = value
        suffix: list[str] = []
        while isinstance(current, ast.Attribute):
            suffix.append(current.attr)
            current = current.value
            if id(current) in self.namespace_values or id(current) in self.unknown_read_values:
                origins, _unknown = self.possible_value_origins(scope, current, seen, member_write=member_write)
                return {".".join((self._origin_name(origin), *reversed(suffix))) for origin in origins}, True
        dotted = _dotted_name(value)
        if dotted is not None:
            name, attributes = dotted
            roots, unknown = self.possible_mutation_roots(scope, name, seen, member_write=member_write)
            if attributes:
                return {".".join((self._origin_name(root), *attributes)) for root in roots}, unknown
            return roots, unknown
        # Unsupported transfers never establish a positive alias. Possible
        # namespace arguments only widen mutation vetoes. Explicit original
        # read/factory facts describe call results; copying a callable's own
        # namespace origin would confuse the reader with its actual target.
        values = (
            [*value.args, *(keyword.value for keyword in value.keywords)]
            if isinstance(value, ast.Call)
            else [child for child in ast.iter_child_nodes(value) if isinstance(child, ast.expr)]
        )
        transferred: set[str | _ClassRef] = set()
        for child in values:
            possible, _unknown = self.possible_value_origins(scope, child, seen, member_write=member_write)
            transferred.update(possible)
        return transferred, True

    def _origin_name(self, root: str | _ClassRef) -> str:
        return root if isinstance(root, str) else self.classes[root].qualified_name

    def possible_mutation_roots(
        self,
        scope: _Scope,
        name: str,
        seen: frozenset[tuple[int, str]] = frozenset(),
        *,
        member_write: bool = False,
    ) -> tuple[set[str | _ClassRef], bool]:
        """Possible import/class origins widen vetoes, never positive proofs."""
        key = (id(scope.node), name)
        if key in seen or len(seen) >= _MAX_VETO_DEPTH:
            return set(), True
        seen = seen | {key}
        roots: set[str | _ClassRef] = set()
        unknown = False
        for candidate, annotation_only in self.lookup_scopes(scope):
            bindings = self._visible_bindings(candidate, name, annotation_only)
            if not bindings:
                continue
            for binding in bindings:
                if binding.imported is not None:
                    roots.add(binding.imported)
                    if binding.imported.endswith(".__getattribute__"):
                        roots.add(f"{_NAMESPACE_ORIGIN}{id(binding.node)}.__getattribute__")
                elif isinstance(binding.node, ast.ClassDef):
                    roots.add(_ClassRef(self.file_path, id(binding.node)))
                else:
                    value = self._assigned_value(binding.node)
                    if value is None:
                        unknown = True
                    else:
                        possible, ambiguous = self.possible_value_origins(candidate, value, seen, member_write=member_write)
                        roots.update(possible)
                        unknown = unknown or ambiguous
            if name not in self.declared_mutable:
                return roots, unknown
        if name in _BUILTIN_BASES or name == "isinstance" or name in _NAMESPACE_FUNCTIONS:
            roots.add(f"builtins.{name}")
        return roots, unknown or not roots


class ExceptionTaxonomy:
    """One finite checked-class graph shared by a selected scan input set."""

    def __init__(self, trees: Mapping[str, ast.Module], *, package_prefix: str = "") -> None:
        self._files: dict[str, _Module] = {}
        self._modules: dict[str, _Module | None] = {}
        self._classes: dict[_ClassRef, _Class] = {}
        self._memo: dict[_ClassRef, bool] = {}
        self._resolving: set[_ClassRef] = set()
        for file_path, tree in trees.items():
            parts = list(PurePosixPath(file_path).with_suffix("").parts)
            if parts and parts[-1] == "__init__":
                parts.pop()
            name = ".".join(part for part in (package_prefix, *parts) if part)
            module = _Module(file_path, name, tree)
            self._files[file_path] = module
            self._modules[name] = None if name in self._modules else module
            self._classes.update(module.classes)
        self._mutations: set[str] = set()
        self._unknown_mutation_members: set[str] = set()
        self._unsupported_namespace = False
        self._namespace_read_closure_complete = True
        self._slot_calls: set[int] = set()
        self._factories: dict[str, _FactorySelection] = {}
        self._read_sources: dict[int, tuple[_Module, ast.expr]] = {}
        self._descriptor_reads: dict[int, _BuiltinDescriptorRead] = {}
        self._instance_dictionary_operations: dict[int, _InstanceDictionaryOperation] = {}
        slots: list[_SlotOperation] = []
        node: ast.Subscript | ast.Call
        for module in self._files.values():
            for scope, call in module.namespace_calls:
                descriptor_read = self._normalize_builtin_descriptor_read(module, call)
                if descriptor_read is not None:
                    self._descriptor_reads[id(call)] = descriptor_read
                operation = self._normalize_slot(module, scope, call)
                if operation is not None:
                    slots.append(operation)
            for scope, node in module.namespace_calls:
                dictionary_operation = self._normalize_instance_dictionary_operation(module, scope, node)
                if dictionary_operation is not None:
                    self._instance_dictionary_operations[id(node)] = dictionary_operation
        self._slot_operations = {id(operation.call): operation for operation in slots}
        # Imported recovery/read aliases may precede their defining input.
        # Reuse only the existing finite veto closure; source facts accumulate
        # before effect classification, independently of module order.
        for _pass in range((len(_READ_CALLABLE_NAMES) + 7) * sum(len(module.namespace_calls) for module in self._files.values()) + 2):
            namespace_before = tuple(
                (
                    frozenset(module.namespace_values),
                    frozenset(module.unknown_read_values),
                    frozenset(module.read_defaults.items()),
                    frozenset(module.namespace_callable_values.items()),
                    frozenset(module.factory_values.items()),
                    frozenset(module.selected_capabilities),
                    frozenset(module.unsupported_read_contexts),
                    frozenset(module.declared_mutable),
                    module.unsupported_namespace,
                )
                for module in self._files.values()
            )
            for module in self._files.values():
                self._read_sources.update({key: (module, node) for key, node in module.namespace_nodes.items()})
            for module in self._files.values():
                self._record_namespace_calls(module)
                self._read_sources.update({key: (module, node) for key, node in module.namespace_nodes.items()})
            namespace_after = tuple(
                (
                    frozenset(module.namespace_values),
                    frozenset(module.unknown_read_values),
                    frozenset(module.read_defaults.items()),
                    frozenset(module.namespace_callable_values.items()),
                    frozenset(module.factory_values.items()),
                    frozenset(module.selected_capabilities),
                    frozenset(module.unsupported_read_contexts),
                    frozenset(module.declared_mutable),
                    module.unsupported_namespace,
                )
                for module in self._files.values()
            )
            if namespace_before == namespace_after:
                break
        else:
            # Incomplete read facts are not evidence of a mutation. They still
            # cannot authorize a checked-kind proof or conceal an effect.
            self._namespace_read_closure_complete = False
        # Store admission consumes completed RHS read facts. Before that closure,
        # a saved dictionary element still carries its raw receiver namespace.
        # Pop calls stay earlier because they supply unknown/default read facts;
        # Store registrations do not feed that closure or establish value aliases.
        for module in self._files.values():
            for scope, node in module.subscript_writes:
                dictionary_operation = self._normalize_instance_dictionary_operation(module, scope, node)
                if dictionary_operation is not None:
                    self._instance_dictionary_operations[id(node)] = dictionary_operation
        for module in self._files.values():
            self._record_namespace_effects(module)
        for module in self._files.values():
            for scope, name, attributes in module.attribute_writes:
                roots, unknown = module.possible_mutation_roots(scope, name, member_write=True)
                for root in roots:
                    target = ".".join((self._reference_name(root), *attributes)) if attributes else root
                    expanded, ambiguous = self._mutation_origins(target, frozenset(), member_write=True)
                    unknown = unknown or ambiguous
                    self._mutations.update(self._reference_name(origin) for origin in expanded)
                if unknown:
                    if attributes:
                        self._unknown_mutation_members.update(attributes)
                    else:
                        module.opaque_namespace = True
            for scope, expression, attributes in module.expression_writes:
                roots, unknown = module.possible_value_origins(scope, expression, member_write=True)
                for root in roots:
                    target = ".".join((self._reference_name(root), *attributes)) if attributes else root
                    expanded, ambiguous = self._mutation_origins(target, frozenset(), member_write=True)
                    unknown = unknown or ambiguous
                    self._mutations.update(self._reference_name(origin) for origin in expanded)
                if unknown:
                    if attributes:
                        self._unknown_mutation_members.update(attributes)
                    else:
                        self._unsupported_namespace = True
            self._unsupported_namespace = self._unsupported_namespace or module.unsupported_namespace
        # All direct facts precede role decisions. Effects only accumulate;
        # a withdrawn role never reappears, independently of module order.
        active = {id(operation.call) for operation in slots}
        for _pass in range(len(slots) + 2):
            before = (frozenset(self._mutations), frozenset(self._unknown_mutation_members), self._unsupported_namespace)
            for operation in slots:
                key = id(operation.call)
                supported = key in active and self._instance_operation_role(operation)
                if not supported:
                    active.discard(key)
                self._record_slot_effect(operation, supported)
            after = (frozenset(self._mutations), frozenset(self._unknown_mutation_members), self._unsupported_namespace)
            if before == after:
                break
        else:
            self._unsupported_namespace = True
        for dictionary_operation in self._instance_dictionary_operations.values():
            if not self._instance_dictionary_operation_valid(dictionary_operation):
                dictionary_operation.module.unsupported_namespace = True
                self._unsupported_namespace = True
        # Checked-class memoization is first populated by queries, after
        # the finite prerequisite withdrawal above has stabilized.
        if not self._unchanged("builtins.globals"):
            for module in self._files.values():
                if module.own_global_writes:
                    # A changed constructor no longer proves a local owner.
                    module.unsupported_namespace = True
                    self._unsupported_namespace = True
        for factory in self._factories.values():
            parent = factory.module.parents.get(id(factory.call))
            if isinstance(parent, ast.Call) and parent.func is factory.call:
                reference = factory.module.effect_reference(factory.call.func)
                if not isinstance(reference, str) or reference not in _REFLECTION_FACTORIES or not self._unchanged(reference):
                    scope = factory.module.scope_of(parent) or factory.module.root
                    factory.module.unsupported_read_contexts.add(id(scope.node))
        for read in self._descriptor_reads.values():
            if not self._builtin_descriptor_provenance_valid(read):
                scope = read.module.scope_of(read.call) or read.module.root
                read.module.record_unknown_read(scope, read.call)
                if not self._builtin_descriptor_identity_unchanged(read.reference, read.member):
                    # Local owner opacity withdraws the read category only;
                    # actual builtin identity/hook changes retain their veto.
                    read.module.unsupported_namespace = True
                    self._unsupported_namespace = True

    def _normalize_instance_dictionary_operation(
        self, module: _Module, scope: _Scope, node: ast.Subscript | ast.Call
    ) -> _InstanceDictionaryOperation | None:
        """Literal declared member effects, never an instance/storage proof."""
        default: ast.expr | None = None
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            dictionary = node.value
            member = self._literal_member(node.slice)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "pop"
            and len(node.args) in {1, 2}
            and not node.keywords
            and not any(isinstance(argument, ast.Starred) for argument in node.args)
        ):
            dictionary = node.func.value
            member = self._literal_member(node.args[0])
            default = node.args[1] if len(node.args) == 2 else None
        else:
            return None
        if (
            not isinstance(dictionary, ast.Attribute)
            or dictionary.attr != "__dict__"
            or not isinstance(dictionary.value, ast.Name)
            or member is None
            or member
            in _NAMESPACE_ATTRIBUTES | _EFFECT_CALLABLE_NAMES | _CLASS_HOOKS | {"__getattribute__", "__class__", "__bases__", "__mro__"}
        ):
            return None
        operation = _InstanceDictionaryOperation(module, scope, node, dictionary.value, member, default)
        return operation if self._instance_dictionary_operation_valid(operation) else None

    def _instance_dictionary_operation_valid(self, operation: _InstanceDictionaryOperation) -> bool:
        """Check lexical receiver/bindings, without class ancestry or purity."""
        module, method = operation.module, operation.scope.node
        if (
            not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            or method.decorator_list
            or method.type_params
            or method.name in _NON_INSTANCE_METHODS
            or module.unsupported_annotation_scope(operation.node)
        ):
            return False
        owner = module.parents.get(id(method))
        positional = [*method.args.posonlyargs, *method.args.args]
        if (
            not isinstance(owner, ast.ClassDef)
            or owner.decorator_list
            or owner.keywords
            or owner.type_params
            or not positional
            or len(method.args.defaults) == len(positional)
        ):
            return False
        first = positional[0].arg
        bindings = operation.scope.bindings.get(first, [])
        owner_scope = module.scope_of(owner)
        reference = _ClassRef(module.file_path, id(owner))
        method_bindings = module.scopes[id(owner)].bindings.get(method.name, [])
        if (
            operation.receiver.id != first
            or self._modules.get(module.name) is not module
            or len(bindings) != 1
            or bindings[0].node is not method.args
            or first in module.declared_mutable
            or owner_scope is None
            or module.lookup(owner_scope, owner.name) != reference
            or len(method_bindings) != 1
            or method_bindings[0].node is not method
            or method.name in module.declared_mutable
            or not self._unchanged(reference)
            or not self._unchanged(f"{self._reference_name(reference)}.{method.name}")
        ):
            return False
        if isinstance(operation.node, ast.Subscript):
            value = module._assigned_value(operation.node)
            if value is None or self._installed_namespace_capability(module, operation.scope, value):
                return False
        current: ast.AST = operation.node
        while current is not method:
            parent = module.parents.get(id(current))
            if parent is None or isinstance(parent, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                return False
            current = parent
        return True

    def _normalize_builtin_descriptor_read(self, module: _Module, call: ast.Call) -> _BuiltinDescriptorRead | None:
        """Exact original builtin descriptor applications, never arbitrary __get__."""
        if (
            not isinstance(call.func, ast.Attribute)
            or call.func.attr != "__get__"
            or len(call.args) != 1
            or call.keywords
            or isinstance(call.args[0], ast.Starred)
            or not isinstance(call.func.value, ast.Subscript)
        ):
            return None
        selected = call.func.value
        dictionary = selected.value
        if not isinstance(dictionary, ast.Attribute) or dictionary.attr != "__dict__":
            return None
        reference = module.effect_reference(dictionary.value)
        member = self._literal_member(selected.slice)
        if (
            not isinstance(reference, str)
            or member is None
            or reference not in _BUILTIN_DESCRIPTOR_READS
            or member not in _BUILTIN_DESCRIPTOR_READS[reference]
        ):
            return None
        receiver_type: ast.Call | None = None
        if reference == "builtins.type":
            receiver = call.args[0]
            if (
                not isinstance(receiver, ast.Call)
                or len(receiver.args) != 1
                or receiver.keywords
                or isinstance(receiver.args[0], ast.Starred)
            ):
                return None
            receiver_type = receiver
        read = _BuiltinDescriptorRead(module, call, dictionary.value, reference, member, receiver_type)
        return read if self._builtin_descriptor_provenance_valid(read) else None

    def _builtin_descriptor_provenance_valid(self, read: _BuiltinDescriptorRead) -> bool:
        # These closed builtin descriptors read data and enforce their own
        # receiver ABI. No custom descriptor/property callback is admitted.
        return (
            "builtins" not in self._modules
            and not read.module.unsupported_annotation_scope(read.call)
            and read.module.effect_reference(read.owner) == read.reference
            and self._builtin_descriptor_identity_unchanged(read.reference, read.member)
            and (
                read.receiver_type is None
                or (read.module.effect_reference(read.receiver_type.func) == "builtins.type" and self._unchanged("builtins.type"))
            )
        )

    def _builtin_descriptor_identity_unchanged(self, reference: str, member: str) -> bool:
        """Instance value uncertainty cannot replace an immutable builtin slot."""
        name = f"{reference}.{member}"
        return (
            member in _BUILTIN_DESCRIPTOR_READS.get(reference, ())
            and self._unchanged(reference)
            and self._unchanged(f"{reference}.__dict__")
            and not any(name == changed or name.startswith(changed + ".") or changed.startswith(name + ".") for changed in self._mutations)
        )

    def _normalize_slot(self, module: _Module, scope: _Scope, call: ast.Call) -> _SlotOperation | None:
        reference = module.effect_reference(call.func)
        builtin = reference in {"builtins.setattr", "builtins.delattr"}
        attribute = call.func if isinstance(call.func, ast.Attribute) and call.func.attr in _SLOT_KINDS else None
        if not builtin and attribute is None:
            if isinstance(call.func, ast.Name) and call.func.id in {"setattr", "delattr"}:
                self._slot_calls.add(id(call))
                module.unsupported_namespace = True
            return None
        self._slot_calls.add(id(call))
        kind: Literal["read", "write", "delete"]
        dependencies: tuple[str, ...] = ()
        binding_dependencies: tuple[tuple[ast.expr, str], ...] = ()
        declared_super = False
        namespace_owner = False
        exception_receiver_abi = False
        if builtin:
            kind = "write" if reference == "builtins.setattr" else "delete"
            offset = 1
            dependencies = (str(reference),)
            binding_dependencies = ((call.func, str(reference)),)
        else:
            assert attribute is not None
            kind = _SLOT_KINDS[attribute.attr]
            owner = module.effect_reference(attribute.value)
            offset = 1 if owner in _UNBOUND_SLOT_OWNERS else 0
            if isinstance(owner, str) and owner in _UNBOUND_SLOT_OWNERS:
                dependencies = (owner, f"{owner}.{attribute.attr}")
                binding_dependencies = ((attribute.value, owner),)
                namespace_owner = owner == "builtins.type"
                exception_receiver_abi = owner in _BUILTIN_EXCEPTION_SLOT_OWNERS
            receiver = attribute.value
            if (
                isinstance(receiver, ast.Call)
                and not receiver.args
                and not receiver.keywords
                and module.effect_reference(receiver.func) == "builtins.super"
            ):
                declared_super = True
                dependencies = ("builtins.super",)
                binding_dependencies = ((receiver.func, "builtins.super"),)
        expected = (2 if kind == "write" else 1) + offset
        if (
            module.unsupported_annotation_scope(call)
            or len(call.args) != expected
            or call.keywords
            or any(isinstance(argument, ast.Starred) for argument in call.args)
        ):
            if kind == "read":
                read_receiver = call.args[0] if offset and call.args and not isinstance(call.args[0], ast.Starred) else receiver
                self._record_unsupported_read(
                    module,
                    scope,
                    call,
                    read_receiver,
                    offset,
                    receiver_keywords=frozenset({"self"}) if offset else frozenset(),
                    member_keywords=frozenset({"name"}),
                )
            else:
                module.unsupported_namespace = True
            return None
        if offset:
            receiver = call.args[0]
        else:
            assert attribute is not None
            receiver = attribute.value
        if kind == "read":
            member = call.args[offset]
            capability = _selected_capability((self._literal_member(member),))
            if capability.effect:
                module.unsupported_namespace = True
            elif capability.namespace_read:
                module.record_namespace_read(scope, call, callable_kind=capability.reader_kind)
            elif self._literal_member(member) is None:
                module.record_unknown_read(scope, call)
        return _SlotOperation(
            module,
            scope,
            call,
            receiver,
            call.args[offset],
            kind,
            dependencies,
            declared_super,
            namespace_owner,
            binding_dependencies,
            exception_receiver_abi,
        )

    def _slot_provenance_valid(self, operation: _SlotOperation) -> bool:
        """Recheck original expressions after named namespace effects, too."""
        return (
            not operation.module.unsupported_annotation_scope(operation.call)
            and ("builtins" not in self._modules or not any(path.startswith("builtins.") for path in operation.dependencies))
            and all(operation.module.effect_reference(expression) == expected for expression, expected in operation.binding_dependencies)
            and all(self._unchanged(dependency) for dependency in operation.dependencies)
        )

    def _declared_owner(self, reference: str | _ClassRef | None, seen: frozenset[_ClassRef] = frozenset()) -> bool:
        """Declared non-namespace owner category; no checked-kind or purity fact."""
        if reference is None or not self._unchanged(reference):
            return False
        if isinstance(reference, str):
            if reference == "builtins.object":
                return True
            if reference.startswith("builtins.") and reference.removeprefix("builtins.") in _BUILTIN_BASES:
                return all(self._declared_owner(f"builtins.{base}", seen) for base in _BUILTIN_BASES[reference.removeprefix("builtins.")])
            module_name, _, local = reference.rpartition(".")
            module = self._modules.get(module_name)
            owned = module.lookup(module.root, local) if module is not None else None
            return isinstance(owned, _ClassRef) and self._declared_owner(owned, seen)
        if reference in seen or len(seen) >= _MAX_VETO_DEPTH:
            return False
        record = self._classes[reference]
        module, node = record.module, record.node
        scope = module.scope_of(node)
        if (
            self._modules.get(module.name) is not module
            or scope is None
            or module.lookup(scope, node.name) != reference
            or node.keywords
            or node.type_params
            or (bool(node.decorator_list) and "typing" in self._modules)
            or any(
                module.effect_reference(decorator) != "typing.final" or not self._unchanged("typing.final")
                for decorator in node.decorator_list
            )
        ):
            return False
        if not node.bases:
            return self._unchanged("builtins.object")
        return all(self._declared_owner(module.effect_reference(base), seen | {reference}) for base in node.bases)

    def _instance_operation_role(self, operation: _SlotOperation) -> bool:
        module, method = operation.module, operation.scope.node
        if not self._slot_provenance_valid(operation) or operation.namespace_owner:
            return False
        if operation.exception_receiver_abi:
            # The intact unbound builtin API accepts an exception instance;
            # this is its ABI, not an annotation/MRO or callback-purity proof.
            return True
        if (
            not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            or method.decorator_list
            or method.type_params
            or method.name in _NON_INSTANCE_METHODS
        ):
            return False
        owner = module.parents.get(id(method))
        positional = [*method.args.posonlyargs, *method.args.args]
        if not isinstance(owner, ast.ClassDef) or not positional or len(method.args.defaults) == len(positional):
            return False
        method_bindings = module.scopes[id(owner)].bindings.get(method.name, [])
        owner_reference = _ClassRef(module.file_path, id(owner))
        if (
            len(method_bindings) != 1
            or method_bindings[0].node is not method
            or method.name in module.declared_mutable
            or not self._unchanged(f"{self._reference_name(owner_reference)}.{method.name}")
        ):
            return False
        first = positional[0].arg
        bindings = operation.scope.bindings.get(first, [])
        if len(bindings) != 1 or bindings[0].node is not method.args or first in module.declared_mutable:
            return False
        current: ast.AST = operation.call
        while current is not method:
            parent = module.parents.get(id(current))
            if parent is None or isinstance(parent, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                return False
            current = parent
        if operation.declared_super:
            if "__class__" in operation.scope.bindings or "__class__" in module.declared_mutable:
                return False
        elif not isinstance(operation.receiver, ast.Name) or operation.receiver.id != first:
            return False
        return self._declared_owner(owner_reference)

    def _record_slot_effect(self, operation: _SlotOperation, instance_role: bool) -> None:
        if not self._slot_provenance_valid(operation):
            if operation.kind == "read":
                operation.module.record_unknown_read(operation.scope, operation.call)
            else:
                self._unsupported_namespace = True
            return
        literal = operation.member.value if isinstance(operation.member, ast.Constant) and isinstance(operation.member.value, str) else None
        if operation.kind == "read":
            selection = _SelectedCapability(
                operation.scope,
                operation.call,
                operation.receiver,
                _selected_capability((literal,)),
                operation.module,
                operation.scope,
            )
            if self._record_selection_effect(operation.module, selection):
                self._unsupported_namespace = True
            return
        if literal in _NAMESPACE_ATTRIBUTES | _NAMESPACE_MUTATORS | _DESCRIPTOR_MUTATORS | _CLASS_HOOKS | {
            "__getattribute__",
            "__class__",
            "__bases__",
            "__mro__",
        }:
            self._unsupported_namespace = True
            return
        roots, unknown = operation.module.possible_value_origins(operation.scope, operation.receiver, member_write=True)
        if any(_namespace_origin(origin) for root in roots for origin in self._mutation_origins(root, frozenset(), member_write=True)[0]):
            self._unsupported_namespace = True
        if literal is None and instance_role and not roots:
            return
        for root in roots:
            target = f"{self._reference_name(root)}.{literal}" if literal is not None else root
            expanded, ambiguous = self._mutation_origins(target, frozenset(), member_write=True)
            unknown = unknown or ambiguous
            self._mutations.update(self._reference_name(origin) for origin in expanded)
        if unknown:
            if literal is None:
                self._unsupported_namespace = True
            else:
                self._unknown_mutation_members.add(literal)

    @staticmethod
    def _literal_member(node: ast.expr) -> str | None:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def _selection_namespace(self, selection: _SelectedCapability, *, member_write: bool = False) -> bool:
        return selection.namespace_bound or any(
            _namespace_origin(root)
            for root in self._expanded_value_origins(
                selection.receiver_module, selection.receiver_scope, selection.receiver, member_write=member_write
            )
        )

    def _record_selection_effect(self, module: _Module, selection: _SelectedCapability) -> bool:
        capability = selection.capability
        if capability.effect or (
            not module.is_identity_comparison(selection.node)
            and (
                (capability.namespace_mutator and self._selection_namespace(selection, member_write=True))
                or any(capability.namespace_path_mutators)
            )
        ):
            module.unsupported_namespace = True
            return True
        return False

    def _record_read_selection(
        self,
        module: _Module,
        scope: _Scope,
        node: ast.expr,
        receiver: ast.expr,
        selectors: tuple[str | None, ...],
        *,
        dotted: bool = False,
        supported: bool = True,
        unproven: bool = False,
        effects: bool = True,
        receiver_context: tuple[_Module, _Scope] | None = None,
        namespace_bound: bool = False,
        default: ast.expr | None = None,
    ) -> None:
        capability = _selected_capability(selectors, dotted=dotted)
        receiver_module, receiver_scope = receiver_context or (module, scope)
        selection = _SelectedCapability(scope, node, receiver, capability, receiver_module, receiver_scope, namespace_bound)
        if effects:
            module.selected_capabilities[id(node), id(receiver), capability, namespace_bound] = selection
        if capability.namespace_read:
            module.record_namespace_read(scope, node, callable_kind=capability.reader_kind)
        module.record_unknown_read(
            scope,
            node,
            default=default,
            withdraw_proof=capability.namespace_read
            or self._selection_namespace(selection)
            or not supported
            or unproven
            or None in selectors,
        )

    def _read_source(self, reference: str | _ClassRef) -> tuple[_Module, ast.expr] | None:
        if not isinstance(reference, str) or not reference.startswith(_NAMESPACE_ORIGIN):
            return None
        token = reference.removeprefix(_NAMESPACE_ORIGIN).partition(".")[0]
        return self._read_sources.get(int(token)) if token.isdecimal() else None

    def _record_unsupported_read(
        self,
        module: _Module,
        scope: _Scope,
        call: ast.Call,
        receiver: ast.expr,
        member_index: int,
        *,
        selectors: tuple[str | None, ...] | None = None,
        dotted: bool = False,
        receiver_context: tuple[_Module, _Scope] | None = None,
        receiver_keywords: frozenset[str] = frozenset(),
        member_keywords: frozenset[str] = frozenset(),
    ) -> None:
        """Keep original selector/argument veto facts, never admit a call shape.

        A positional selector is anchored only before any expansion. Explicit
        other arguments/defaults remain possible veto sources; a declared
        receiver position or keyword is excluded from result provenance, but
        explicit named receivers still participate in veto-only selection.
        Expansions are not evaluated, and unknown alone is not a namespace.
        """
        prefix = call.args[:member_index]
        receiver_anchored = len(prefix) == member_index and not any(isinstance(argument, ast.Starred) for argument in prefix)
        if selectors is None:
            member = (
                call.args[member_index]
                if len(call.args) > member_index and receiver_anchored and not isinstance(call.args[member_index], ast.Starred)
                else None
            )
            selectors = (self._literal_member(member),) if member is not None else ()
            selectors += tuple(self._literal_member(keyword.value) for keyword in call.keywords if keyword.arg in member_keywords)
            if not selectors:
                selectors = (None,)
        arguments = call.args[member_index:] if receiver_anchored else call.args
        module.record_unknown_read(
            scope,
            call,
            veto_sources=(*arguments, *(keyword.value for keyword in call.keywords if keyword.arg not in receiver_keywords)),
        )
        named_receivers = tuple(keyword.value for keyword in call.keywords if keyword.arg in receiver_keywords)
        receivers: tuple[tuple[ast.expr, tuple[_Module, _Scope] | None], ...] = (
            ((receiver, receiver_context),) if receiver is not call or not named_receivers else ()
        )
        receivers += tuple((named_receiver, None) for named_receiver in named_receivers)
        for selected_receiver, selected_context in receivers:
            self._record_read_selection(
                module,
                scope,
                call,
                selected_receiver,
                selectors,
                dotted=dotted,
                supported=False,
                unproven=True,
                receiver_context=selected_context,
            )

    def _record_namespace_calls(self, module: _Module) -> None:
        for scope, expression in module.callable_recoveries:
            origins, unknown = module.possible_value_origins(scope, expression)
            for origin in origins:
                _expanded, ambiguous = self._mutation_origins(origin, frozenset())
                unknown = unknown or ambiguous
            if unknown or not origins:
                module.record_namespace_read(scope, expression, callable_kind=expression.attr)
        for scope, selection in module.namespace_selections:
            if any(_namespace_origin(root) for root in self._expanded_value_origins(module, scope, selection.value)):
                self._record_read_selection(module, scope, selection, selection.value, (self._literal_member(selection.slice),))
        for scope, node in module.namespace_calls:
            dictionary_operation = self._instance_dictionary_operations.get(id(node))
            if dictionary_operation is not None:
                module.record_unknown_read(scope, node, default=dictionary_operation.default)
                continue
            read = self._descriptor_reads.get(id(node))
            if read is not None and self._builtin_descriptor_provenance_valid(read):
                module.record_unknown_read(scope, node, withdraw_proof=False)
                continue
            if id(node) in self._slot_calls:
                operation = self._slot_operations.get(id(node))
                if operation is not None and operation.kind == "read":
                    self._record_read_selection(
                        module,
                        scope,
                        node,
                        operation.receiver,
                        (self._literal_member(operation.member),),
                        supported=self._slot_provenance_valid(operation),
                        effects=False,
                    )
                continue
            functions, unknown_function = module.possible_value_origins(scope, node.func)
            expanded: set[str | _ClassRef] = set()
            for function in functions:
                origins, ambiguous = self._mutation_origins(function, frozenset())
                unknown_function = unknown_function or ambiguous
                expanded.update(origins)
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "__get__") or any(
                isinstance(function, str) and function.endswith(".__get__") for function in expanded
            ):
                # An unproved descriptor read supplies no purity or namespace
                # result fact, including a read callable carried by an alias.
                module.record_unknown_read(scope, node)
            mapping_methods = _NAMESPACE_READ_METHODS - _READ_CALLABLE_NAMES
            lookups = [
                function
                for function in expanded
                if _namespace_origin(function) and isinstance(function, str) and function.rsplit(".", 1)[-1] in mapping_methods
            ]
            if lookups:
                lookup = any(function.rsplit(".", 1)[-1] in {"get", "__getitem__"} for function in lookups)
                if lookup:
                    receiver = node.func.value if isinstance(node.func, ast.Attribute) else node.func
                    self._record_read_selection(
                        module,
                        scope,
                        node,
                        receiver,
                        (self._literal_member(node.args[0]) if node.args else None,),
                        supported=len(node.args) == 1 and not node.keywords and not isinstance(node.args[0], ast.Starred),
                        namespace_bound=True,
                        default=node.args[1] if len(node.args) == 2 else None,
                    )
                else:
                    module.record_namespace_read(scope, node)
            constructors = {
                function.rsplit(".", 1)[-1]
                for function in expanded
                if isinstance(function, str)
                and (function in {f"builtins.{name}" for name in _NAMESPACE_CONSTRUCTORS} or _namespace_origin(function))
                and function.rsplit(".", 1)[-1] in _NAMESPACE_CONSTRUCTORS
            }
            if constructors.intersection({"globals", "locals"}) or ("vars" in constructors and not node.args):
                parent = module.parents.get(id(node))
                if id(parent) in module.own_global_writes:
                    continue
                if (
                    isinstance(parent, ast.Subscript)
                    and isinstance(parent.ctx, (ast.Store, ast.Del))
                    and isinstance(parent.slice, ast.Constant)
                    and isinstance(parent.slice.value, str)
                ):
                    module.declared_mutable.add(parent.slice.value)
                module.record_namespace_read(scope, node)
            if "vars" in constructors and node.args:
                module.record_namespace_read(scope, node)
            if expanded.intersection({"builtins.setattr", "builtins.delattr"}) and node.args:
                module.unsupported_namespace = True
            if expanded.intersection({"builtins.exec", "builtins.eval"}) or (
                isinstance(node.func, ast.Name) and node.func.id in {"exec", "eval"}
            ):
                module.unsupported_namespace = True
            self._record_reflection_recovery(module, node, expanded, unknown_function)

    def _expanded_value_origins(
        self, module: _Module, scope: _Scope, value: ast.expr, *, member_write: bool = False
    ) -> set[str | _ClassRef]:
        roots, _unknown = module.possible_value_origins(scope, value, member_write=member_write)
        expanded: set[str | _ClassRef] = set()
        for root in roots:
            origins, _ambiguous = self._mutation_origins(root, frozenset(), member_write=member_write)
            expanded.update(origins)
        return expanded

    def _record_namespace_effects(self, module: _Module) -> None:
        """Actual namespace effects stay graph refusals, including alias chains.

        Passive arguments to unrelated calls are not modeled writes. Literal
        data receiver identity remains separate from contained-object vetoes.
        Direct declared instance dictionary operations use member effects;
        the dictionary's namespace facts remain intact for other consumers.
        """
        for scope, reference in module.namespace_method_recoveries:
            parent = module.parents.get(id(reference))
            # The exact declared pop has its own member-effect owner. Other
            # literal mutators retain the same veto when invoked or escaped.
            if isinstance(parent, ast.Call) and parent.func is reference and id(parent) in self._instance_dictionary_operations:
                continue
            self._record_selection_effect(
                module,
                _SelectedCapability(
                    scope,
                    reference,
                    reference.value,
                    _selected_capability((reference.attr,)),
                    module,
                    scope,
                ),
            )
        for selection in module.selected_capabilities.values():
            self._record_selection_effect(module, selection)
        for operation in self._instance_dictionary_operations.values():
            if operation.module is module:
                # Same conservative member inventory as receiver.member = ... .
                # The dictionary remains a namespace for every other consumer.
                self._unknown_mutation_members.add(operation.member)
        for scope, node in module.subscript_writes:
            if id(node) in self._instance_dictionary_operations:
                continue
            if id(node) in module.own_global_writes:
                parent = module.parents.get(id(node))
                value = parent.value if isinstance(parent, ast.AugAssign) else module._assigned_value(node)
                unsupported_store = isinstance(node.ctx, ast.Store) and value is None and not isinstance(parent, ast.AnnAssign)
                if unsupported_store or (value is not None and self._installed_namespace_capability(module, scope, value)):
                    # Do not lose a namespace/callable edge installed through
                    # a dynamic global binding, including compound targets.
                    # Unmodeled stores have no proved RHS; deletion stays local.
                    # This is a veto, not an alias.
                    module.unsupported_namespace = True
                member = self._literal_member(node.slice)
                if member is None:
                    module.opaque_namespace = True
                else:
                    module.declared_mutable.add(member)
            elif any(_namespace_origin(root) for root in self._expanded_value_origins(module, scope, node.value)):
                module.unsupported_namespace = True
        for scope, value, _members in module.expression_writes:
            if any(_namespace_origin(root) for root in self._expanded_value_origins(module, scope, value, member_write=True)):
                module.unsupported_namespace = True
        for scope, name, _attributes in module.attribute_writes:
            roots, _unknown = module.possible_mutation_roots(scope, name, member_write=True)
            if any(
                _namespace_origin(origin) for root in roots for origin in self._mutation_origins(root, frozenset(), member_write=True)[0]
            ):
                module.unsupported_namespace = True
        for scope, call in module.namespace_calls:
            if id(call) in self._instance_dictionary_operations:
                continue
            read = self._descriptor_reads.get(id(call))
            if read is not None and self._builtin_descriptor_provenance_valid(read):
                continue
            if id(call) in self._slot_calls:
                continue
            functions = self._expanded_value_origins(module, scope, call.func)
            namespace_functions = [
                root for root in functions if _namespace_origin(root) and isinstance(root, str) and not root.endswith(".__get__")
            ]
            if not namespace_functions:
                continue
            reader = any(isinstance(root, str) and root.rsplit(".", 1)[-1] in _READ_CALLABLE_NAMES for root in namespace_functions)
            selectors = call.args if reader else call.args[:1]
            if any(
                not isinstance(root, str) or root.rsplit(".", 1)[-1] not in _NAMESPACE_READ_METHODS for root in namespace_functions
            ) or any(_selected_capability((self._literal_member(selector),)).effect for selector in selectors):
                module.unsupported_namespace = True

    def _installed_namespace_capability(self, module: _Module, scope: _Scope, value: ast.expr) -> bool:
        return any(
            _namespace_origin(root)
            or (
                isinstance(root, str)
                and (
                    root.startswith(_FACTORY_ORIGIN)
                    or root in _REFLECTION_GETTERS | _REFLECTION_FACTORIES
                    or root in {f"builtins.{name}" for name in _NAMESPACE_FUNCTIONS}
                )
            )
            for root in self._expanded_value_origins(module, scope, value)
        )

    def _record_reflection_recovery(self, module: _Module, node: ast.Call, functions: set[str | _ClassRef], unknown_function: bool) -> None:
        scope = module.scope_of(node) or module.root
        factories = [self._factories[reference] for reference in functions if isinstance(reference, str) and reference in self._factories]
        if factories:
            if len(node.args) != 1 or node.keywords or isinstance(node.args[0], ast.Starred):
                receiver = node.args[0] if node.args and not isinstance(node.args[0], ast.Starred) else node
                for factory in factories:
                    self._record_unsupported_read(module, scope, node, receiver, 1, selectors=factory.selectors, dotted=True)
                return
            for factory in factories:
                # The tag stays veto-only. Only this original application can
                # distinguish a canonical constructor from the tag's deliberate
                # not-a-type-alias ambiguity; final effects recheck its API.
                original_canonical = (
                    factory.module is module
                    and node.func is factory.call
                    and factory.supported
                    and factory.canonical_constructor
                    and not module.unsupported_annotation_scope(factory.call)
                    and module.effect_reference(factory.call.func) in _REFLECTION_FACTORIES
                )
                self._record_read_selection(
                    module,
                    scope,
                    node,
                    node.args[0],
                    factory.selectors,
                    dotted=True,
                    supported=factory.supported,
                    unproven=unknown_function and not original_canonical,
                )
            return
        internal_factory = any(
            _namespace_origin(reference) and isinstance(reference, str) and reference.endswith(".attrgetter") for reference in functions
        )
        if functions.intersection(_REFLECTION_FACTORIES) or internal_factory:
            selectors = tuple(self._literal_member(argument) for argument in node.args)
            supported = bool(node.args) and not node.keywords and not any(isinstance(argument, ast.Starred) for argument in node.args)
            tag = f"{_FACTORY_ORIGIN}{id(node)}"
            self._factories[tag] = _FactorySelection(
                module, node, selectors, supported, not unknown_function and functions == _REFLECTION_FACTORIES
            )
            module.factory_values[id(node)] = tag
            capability = _selected_capability(selectors, dotted=True)
            if capability.effect:
                module.unsupported_namespace = True
            if unknown_function or not supported or capability.namespace_read or None in selectors:
                module.unsupported_read_contexts.add(id(scope.node))
            return
        internal_getter = any(
            _namespace_origin(reference) and isinstance(reference, str) and reference.rsplit(".", 1)[-1] in {"getattr", "getattr_static"}
            for reference in functions
        )
        if (
            functions.intersection(_REFLECTION_GETTERS)
            or internal_getter
            or (isinstance(node.func, ast.Name) and node.func.id == "getattr")
        ):
            if len(node.args) not in {2, 3} or node.keywords or any(isinstance(argument, ast.Starred) for argument in node.args):
                receiver = node.args[0] if node.args and not isinstance(node.args[0], ast.Starred) else node
                self._record_unsupported_read(
                    module,
                    scope,
                    node,
                    receiver,
                    1,
                    receiver_keywords=frozenset({"obj", "object"}),
                    member_keywords=frozenset({"name", "attr"}),
                )
                return
            self._record_read_selection(
                module,
                scope,
                node,
                node.args[0],
                (self._literal_member(node.args[1]),),
                supported=True,
                unproven=unknown_function,
                default=node.args[2] if len(node.args) == 3 else None,
            )
            return
        getter_references = [reference for reference in functions if isinstance(reference, str) and reference.endswith(".__getattribute__")]
        if not getter_references:
            return
        recovered = False
        for reference in getter_references:
            source = self._read_source(reference)
            if source is None:
                continue
            source_module, original = source
            if not isinstance(original, ast.Attribute) or original.attr != "__getattribute__":
                continue
            unbound = source_module.effect_reference(original.value) in _UNBOUND_SLOT_OWNERS
            offset = 1 if unbound else 0
            receiver = node.args[0] if unbound and node.args and not isinstance(node.args[0], ast.Starred) else original.value
            receiver_context = None if unbound else (source_module, source_module.scope_of(original) or source_module.root)
            if len(node.args) != offset + 1 or node.keywords or any(isinstance(argument, ast.Starred) for argument in node.args):
                self._record_unsupported_read(
                    module,
                    scope,
                    node,
                    receiver,
                    offset,
                    receiver_context=receiver_context,
                    receiver_keywords=frozenset({"self"}) if unbound else frozenset(),
                    member_keywords=frozenset({"name"}),
                )
                recovered = True
                continue
            self._record_read_selection(
                module,
                scope,
                node,
                receiver,
                (self._literal_member(node.args[offset]),),
                unproven=unknown_function,
                receiver_context=receiver_context,
            )
            recovered = True
        if not recovered:
            # Preserve literal veto selections from an explicitly recovered
            # slot reader, without proving its owner or a favorable alias.
            # The two declared call shapes end with the selected member.
            if len(node.args) in {1, 2} and not node.keywords and not any(isinstance(argument, ast.Starred) for argument in node.args):
                self._record_read_selection(
                    module,
                    scope,
                    node,
                    node.args[0] if len(node.args) == 2 else node,
                    (self._literal_member(node.args[-1]),),
                    unproven=True,
                )
            else:
                offset = 1 if len(node.args) >= 2 else 0
                receiver = node.args[0] if offset and not isinstance(node.args[0], ast.Starred) else node
                self._record_unsupported_read(
                    module,
                    scope,
                    node,
                    receiver,
                    offset,
                    receiver_keywords=frozenset({"self"}) if offset else frozenset(),
                    member_keywords=frozenset({"name"}),
                )

    def _mutation_origins(
        self,
        reference: str | _ClassRef,
        seen: frozenset[str | _ClassRef],
        followed_exports: frozenset[tuple[int, str]] = frozenset(),
        *,
        member_write: bool = False,
    ) -> tuple[set[str | _ClassRef], bool]:
        """Close supplied-unit export/assignment edges only to widen vetoes."""
        if reference in seen or len(followed_exports) >= _MAX_VETO_DEPTH:
            return {reference}, True
        roots = {reference}
        if not isinstance(reference, str):
            return roots, False
        if reference.endswith(".__dict__"):
            expanded, unknown = self._mutation_origins(
                reference.removesuffix(".__dict__"), seen | {reference}, followed_exports, member_write=member_write
            )
            return roots | expanded, unknown
        parts = reference.split(".")
        for length in range(len(parts) - 1, 0, -1):
            module_name = ".".join(parts[:length])
            if module_name not in self._modules:
                continue
            module = self._modules[module_name]
            if module is None:
                return roots, True
            edge = (id(module.tree), parts[length])
            if edge in followed_exports:
                return roots, True
            possible, unknown = module.possible_mutation_roots(module.root, parts[length], member_write=member_write)
            tail = parts[length + 1 :]
            for origin in possible:
                if tail and isinstance(origin, _ClassRef):
                    roots.add(".".join((self._reference_name(origin), *tail)))
                    continue
                target = ".".join((self._reference_name(origin), *tail)) if tail else origin
                expanded, ambiguous = self._mutation_origins(
                    target, seen | {reference}, followed_exports | {edge}, member_write=member_write
                )
                roots.update(expanded)
                unknown = unknown or ambiguous
            return roots, unknown
        return roots, False

    def _reference_name(self, reference: str | _ClassRef) -> str:
        return reference if isinstance(reference, str) else self._classes[reference].qualified_name

    def _unchanged(self, reference: str | _ClassRef) -> bool:
        name = self._reference_name(reference)
        if name.rsplit(".", 1)[-1] in self._unknown_mutation_members or self._unknown_mutation_members.intersection(
            _CLASS_HOOKS | {"__bases__", "__class__", "__mro__"}
        ):
            return False
        return not any(name == changed or name.startswith(changed + ".") or changed.startswith(name + ".") for changed in self._mutations)

    def _is_exception(self, reference: str | _ClassRef | None) -> bool:
        if reference is None or not self._unchanged(reference):
            return False
        if isinstance(reference, str):
            if reference.startswith("builtins.") and "builtins" in self._modules:
                return False
            module_name, separator, local = reference.rpartition(".")
            module = self._modules.get(module_name) if separator else None
            if (reference.startswith("builtins.") or module_name not in self._modules) and reference in _EXTERNAL_BASES:
                if not reference.startswith("builtins.") and any(module_name.startswith(owned + ".") for owned in self._modules):
                    return False
                return all(self._is_exception(base) for base in _EXTERNAL_BASES[reference])
            if module is None:
                return False
            owned = module.lookup(module.root, local)
            return isinstance(owned, _ClassRef) and self._is_exception(owned)
        if reference in self._memo:
            return self._memo[reference]
        if reference in self._resolving:
            return False
        record = self._classes[reference]
        node = record.node
        if (
            self._modules.get(record.module.name) is not record.module
            or node.decorator_list
            or node.keywords
            or node.type_params
            or not node.bases
            or _CLASS_HOOKS.intersection(record.module.scopes[id(node)].bindings)
        ):
            self._memo[reference] = False
            return False
        self._resolving.add(reference)
        try:
            result = all(self._is_exception(record.module.resolve(base)) for base in node.bases)
        finally:
            self._resolving.remove(reference)
        self._memo[reference] = result
        return result

    def is_exception_kind(self, file_path: str, call: ast.Call, qualified_import: Callable[[ast.expr], str | None]) -> bool:
        """Prove a supported builtin call's checked class/tuple, never its subject."""
        module = self._files.get(file_path)
        if (
            module is None
            or self._unsupported_namespace
            or not self._namespace_read_closure_complete
            or module.has_unsupported_read_context(call)
            or len(call.args) != 2
            or call.keywords
            or any(isinstance(argument, ast.Starred) for argument in call.args)
            or not isinstance(call.func, ast.Name)
        ):
            return False
        if module.unsupported_annotation_scope(call):
            return False
        if (
            call.func.id != "isinstance"
            or module.resolve(call.func) != "builtins.isinstance"
            or not self._unchanged("builtins.isinstance")
            or not (module.builtin_fallback(call.func) or qualified_import(call.func) == "builtins.isinstance")
        ):
            return False
        return self._checked_operand(module, call.args[1], qualified_import)

    def _checked_operand(self, module: _Module, node: ast.expr, qualified_import: Callable[[ast.expr], str | None]) -> bool:
        if isinstance(node, ast.Tuple):
            return bool(node.elts) and all(self._checked_operand(module, leaf, qualified_import) for leaf in node.elts)
        reference = module.resolve(node)
        if isinstance(reference, str) and qualified_import(node) != reference and not module.builtin_fallback(node):
            return False
        return self._is_exception(reference)
