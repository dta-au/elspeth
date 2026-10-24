"""Local dialect dispatch with lexical builder and selector evidence.

Import/control effects reuse the shared evaluator. This rule additionally
tracks explicit dialect snapshots and the class-local validation conventions
used by SQLAlchemy authorities. It does not resolve arbitrary calls, external
attribute mutation, inheritance, or higher-order factory invocation.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from elspeth_lints.core.ast_walker import iter_own_scope
from elspeth_lints.core.boundary_aliases import (
    ImportAliasEffect,
    argument_names,
    assignment_target_names,
    evaluate_alias_flow,
    evaluate_finally_entry_aliases,
    function_local_binding_names,
    identical_alias_join,
    import_alias_effect,
    match_pattern_binding_names,
    possibly_bound_names,
)

SQLITE_MODULE = "sqlalchemy.dialects.sqlite"
POSTGRESQL_MODULE = "sqlalchemy.dialects.postgresql"
SUPPORTED_DIALECTS = frozenset({"sqlite", "postgresql"})
# Positive builtin identities, masked by ordinary lexical binders/imports.
_EAGER_CONSUMERS = frozenset(f"builtins.{name}" for name in ("list", "tuple", "set", "frozenset", "dict", "sorted"))
_BUILTINS = {name.rpartition(".")[2]: name for name in (*_EAGER_CONSUMERS, "builtins.staticmethod", "builtins.classmethod")}

type Dispatch = ast.If | ast.IfExp


@dataclass(frozen=True, slots=True)
class Guard:
    """One arm's captured selector, lexical dependencies and selected dialect."""

    selector: str
    dependencies: frozenset[str]
    dialect: str


@dataclass(frozen=True, slots=True)
class BuilderCall:
    """A proven imported builder and its nearest explicit dispatch arm."""

    node: ast.Call
    name: str
    dialect: str
    owner: Dispatch | None
    guard: Guard | None


@dataclass(frozen=True, slots=True)
class BuilderAnalysis:
    calls: tuple[BuilderCall, ...]
    compared_dialects: frozenset[str]


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _absolute_target(node: ast.expr, bindings: dict[str, str]) -> str | None:
    dotted = _dotted(node)
    if dotted is None:
        return None
    root, _, suffix = dotted.partition(".")
    imported = bindings.get(root)
    return None if imported is None else imported + (f".{suffix}" if suffix else "")


def _dialect_receiver(node: ast.expr) -> ast.expr | None:
    if isinstance(node, ast.Attribute) and node.attr == "name" and isinstance(node.value, ast.Attribute) and node.value.attr == "dialect":
        return node.value.value
    return None


def _literals(node: ast.expr) -> frozenset[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return frozenset((node.value,))
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values: set[str] = set()
        for item in node.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return frozenset()
            values.add(item.value)
        return frozenset(values)
    return frozenset()


def _site(node: ast.AST) -> str:
    if isinstance(node, (ast.stmt, ast.expr, ast.arg)):
        return f"{node.lineno}:{node.col_offset}"
    raise AssertionError("Binding sites require a positioned syntax node")


def _instance_receiver(node: ast.FunctionDef | ast.AsyncFunctionDef, bindings: dict[str, str]) -> str | None:
    if any(_absolute_target(decorator, bindings) in {"builtins.staticmethod", "builtins.classmethod"} for decorator in node.decorator_list):
        return None
    positional = (*node.args.posonlyargs, *node.args.args)
    return positional[0].arg if positional else None


@dataclass(slots=True)
class _Facts:
    """Rule-owned selector facts, separate from absolute object bindings."""

    bindings: dict[str, str] = field(default_factory=dict)
    selectors: dict[str, str] = field(default_factory=dict)
    receivers: dict[str, str] = field(default_factory=dict)
    cached: dict[str, str] = field(default_factory=dict)
    engines: dict[str, frozenset[str]] = field(default_factory=dict)
    domains: dict[str, frozenset[str]] = field(default_factory=dict)

    def copy(self) -> _Facts:
        return _Facts(
            dict(self.bindings), dict(self.selectors), dict(self.receivers), dict(self.cached), dict(self.engines), dict(self.domains)
        )

    def receiver(self, path: str) -> str:
        parts = path.split(".")
        for count in range(len(parts), 0, -1):
            prefix = ".".join(parts[:count])
            if prefix in self.receivers:
                return ".".join((self.receivers[prefix], *parts[count:]))
        return f"global:{path}"

    def direct_selector(self, receiver: ast.expr) -> str | None:
        path = _dotted(receiver)
        if path is None:
            return None
        origin = f"direct:{self.receiver(path)}"
        if path in self.engines:
            self.domains[origin] = self.engines[path]
        return origin

    def selector(self, node: ast.expr) -> str | None:
        receiver = _dialect_receiver(node)
        if receiver is not None:
            return self.direct_selector(receiver)
        if isinstance(node, ast.Name):
            return self.selectors.get(node.id)
        dotted = _dotted(node)
        return self.cached.get(dotted) if dotted is not None else None

    def invalidate(self, names: Sequence[str], node: ast.AST) -> None:
        for name in names:
            self.bindings.pop(name, None)
            self.selectors.pop(name, None)
            self.replace_receiver(name, f"binding:{_site(node)}:{name}")
        self.cached = {path: origin for path, origin in self.cached.items() if path.partition(".")[0] not in names}
        self.engines = {path: domain for path, domain in self.engines.items() if path.partition(".")[0] not in names}

    def replace_receiver(self, path: str, origin: str) -> None:
        self.receivers = {name: value for name, value in self.receivers.items() if not name.startswith(path + ".")}
        self.receivers[path] = origin

    def target(self, target: ast.expr, node: ast.AST) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self.target(element, node)
            return
        if isinstance(target, ast.Starred):
            self.target(target.value, node)
            return
        self.invalidate(assignment_target_names(target), node)
        if isinstance(target, ast.Attribute):
            path = _dotted(target)
            if path is not None:
                self.bindings.pop(path.partition(".")[0], None)
                self.cached = {name: origin for name, origin in self.cached.items() if name != path and not name.startswith(path + ".")}
                self.engines = {name: domain for name, domain in self.engines.items() if name != path and not name.startswith(path + ".")}
                receiver_path = path
                if path.endswith(".dialect.name"):
                    receiver_path = path.removesuffix(".dialect.name")
                elif path.endswith((".engine", ".dialect")):
                    receiver_path = path.rpartition(".")[0]
                self.replace_receiver(receiver_path, f"attribute:{_site(node)}:{receiver_path}")

    def assign(self, targets: Sequence[ast.expr], origin: str | None, node: ast.AST) -> None:
        for target in targets:
            self.target(target, node)
            if isinstance(target, ast.Name) and origin is not None:
                self.selectors[target.id] = origin


def _normal_bindings(body: Sequence[ast.stmt], incoming: dict[str, str]) -> dict[str, str]:
    return identical_alias_join(tuple(path.aliases for path in evaluate_alias_flow(body, incoming) if path.transfer is None))


def _join_expression_facts(node: ast.expr, paths: Sequence[_Facts]) -> _Facts:
    """Retain selector evidence shared by all optional expression paths."""
    if not paths:
        raise AssertionError("Expression joins require an execution path")
    first = paths[0]
    facts = _Facts(
        bindings=identical_alias_join(tuple(path.bindings for path in paths)),
        selectors=identical_alias_join(tuple(path.selectors for path in paths)),
        receivers=identical_alias_join(tuple(path.receivers for path in paths)),
        cached=identical_alias_join(tuple(path.cached for path in paths)),
        engines={name: domain for name, domain in first.engines.items() if all(path.engines.get(name) == domain for path in paths[1:])},
        domains={name: domain for name, domain in first.domains.items() if all(path.domains.get(name) == domain for path in paths[1:])},
    )
    for name in {name for path in paths for name in path.receivers} - facts.receivers.keys():
        # A varying local must not fall back to a same-spelled module name.
        facts.receivers[name] = f"conditional:{_site(node)}:{name}"
    return facts


def _comprehension_effects(node: ast.expr, start: _Facts, paths: Sequence[_Facts], targets: set[str]) -> _Facts:
    """Project enclosing walrus writes without publishing iteration locals."""
    joined = _join_expression_facts(node, paths)
    result = start.copy()
    for before, after in (
        (result.bindings, joined.bindings),
        (result.selectors, joined.selectors),
        (result.receivers, joined.receivers),
        (result.cached, joined.cached),
    ):
        for name in tuple(before):
            if name.partition(".")[0] in targets:
                del before[name]
        before.update({name: value for name, value in after.items() if name.partition(".")[0] in targets})
    result.engines = {name: value for name, value in start.engines.items() if name.partition(".")[0] not in targets}
    result.engines.update({name: value for name, value in joined.engines.items() if name.partition(".")[0] in targets})
    result.domains = dict(joined.domains)
    return result


def _invalidate_attribute_targets(statements: Iterable[ast.stmt], facts: _Facts, site: ast.AST) -> None:
    for statement in statements:
        for child in iter_own_scope(statement):
            if isinstance(child, ast.Attribute) and isinstance(child.ctx, (ast.Store, ast.Del)):
                facts.target(child, site)


def _compound_facts(node: ast.stmt, incoming: _Facts) -> _Facts:
    """Use the existing evaluator to invalidate only reachable binding effects."""
    names, star = possibly_bound_names((node,))
    if star:
        names.update(
            (
                *incoming.bindings,
                *incoming.selectors,
                *(path.partition(".")[0] for path in (*incoming.cached, *incoming.engines, *incoming.receivers)),
            )
        )
    tokens = {name: f"binding-sentinel:{name}" for name in names}
    normal = _normal_bindings((node,), tokens)
    changed = tuple(name for name in names if normal.get(name) != tokens[name])
    facts = incoming.copy()
    facts.invalidate(changed, node)
    _invalidate_attribute_targets((node,), facts, node)
    return facts


def _rejection(node: ast.If, facts: _Facts) -> None:
    """Recognize a dominating rejecting membership or same-engine guard."""
    if node.orelse or not node.body or not isinstance(node.body[-1], ast.Raise):
        return
    test = node.test
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == len(test.comparators) == 1
        and isinstance(test.ops[0], ast.NotIn)
        and isinstance(test.comparators[0], (ast.List, ast.Set, ast.Tuple))
    ):
        origin = facts.selector(test.left)
        domain = _literals(test.comparators[0])
        if origin is not None and domain:
            facts.domains[origin] = domain
    # On fallthrough from an OR rejection, every disjunct is false. AND
    # cannot establish the same-engine relation and is deliberately excluded.
    terms = test.values if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or) else (test,)
    for term in terms:
        if not isinstance(term, ast.Compare) or len(term.ops) != 1 or len(term.comparators) != 1 or not isinstance(term.ops[0], ast.IsNot):
            continue
        left, right = term.left, term.comparators[0]
        for connection, engine in ((left, right), (right, left)):
            if not isinstance(connection, ast.Attribute) or connection.attr != "engine":
                continue
            engine_path = _dotted(engine)
            if engine_path is not None and engine_path in facts.engines:
                origin = facts.direct_selector(connection.value)
                if origin is not None:
                    facts.domains[origin] = facts.engines[engine_path]


def _returned_function(body: Sequence[ast.stmt]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    if not body or not isinstance(body[-1], ast.Return) or not isinstance(body[-1].value, ast.Name):
        return None
    name = body[-1].value.id
    definitions = [node for node in body[:-1] if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    if len(definitions) != 1:
        return None
    definition = definitions[0]
    rebound, star = possibly_bound_names(body[body.index(definition) + 1 : -1])
    return definition if not star and name not in rebound else None


@dataclass(frozen=True, slots=True)
class _ClassCache:
    selectors: dict[str, frozenset[str] | None]
    engines: dict[str, frozenset[str]]


class _ReceiverWrites(ast.NodeVisitor):
    def __init__(self, receiver: str) -> None:
        self.receiver = receiver
        self.writes: list[ast.Attribute] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)) and isinstance(node.value, ast.Name) and node.value.id == self.receiver:
            self.writes.append(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self.receiver not in function_local_binding_names(node):
            for statement in node.body:
                self.visit(statement)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if self.receiver not in function_local_binding_names(node):
            for statement in node.body:
                self.visit(statement)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Method parameters mask the receiver in their own visitor. A class
        # suite itself executes now and may still write a captured receiver.
        for statement in node.body:
            self.visit(statement)


def _class_cache(node: ast.ClassDef, bindings: dict[str, str], deferred_bindings: dict[str, str]) -> _ClassCache:
    method_bindings: dict[ast.FunctionDef | ast.AsyncFunctionDef, dict[str, str]] = {}
    current = dict(bindings)
    for statement in node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method_bindings[statement] = dict(current)
        current = _normal_bindings((statement,), current)
    initializers = [child for child in node.body if isinstance(child, ast.FunctionDef) and child.name == "__init__"]
    if len(initializers) != 1:
        return _ClassCache({}, {})
    initializer = initializers[0]
    receiver = _instance_receiver(initializer, method_bindings[initializer])
    if receiver is None:
        return _ClassCache({}, {})
    facts = _Facts(dict(deferred_bindings))
    facts.invalidate(tuple(function_local_binding_names(initializer)), initializer)
    proven: dict[str, ast.Attribute] = {}
    selectors: dict[str, frozenset[str] | None] = {}
    engines: dict[str, frozenset[str]] = {}
    for statement in initializer.body:
        incoming_bindings = dict(facts.bindings)
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
            value = statement.value
            targets = statement.targets if isinstance(statement, ast.Assign) else (statement.target,)
            facts = _BindingVisitor.expression_facts(value, facts)
            origin = facts.selector(value)
            engine_origin = facts.direct_selector(value) if isinstance(value, (ast.Name, ast.Attribute)) else None
            for target in targets:
                facts = _BindingVisitor.expression_facts(target, facts)
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == receiver:
                    if origin is not None:
                        proven[target.attr] = target
                        selectors[target.attr] = facts.domains.get(origin)
                    elif engine_origin is not None and engine_origin in facts.domains:
                        proven[target.attr] = target
                        engines[target.attr] = facts.domains[engine_origin]
            facts.assign(targets, origin, statement)
            if isinstance(statement, ast.AnnAssign):
                facts = _BindingVisitor.expression_facts(statement.annotation, facts)
        elif isinstance(statement, ast.Expr):
            facts = _BindingVisitor.expression_facts(statement.value, facts)
        else:
            facts = _compound_facts(statement, facts)
            if isinstance(statement, ast.If):
                _rejection(statement, facts)
        facts.bindings = _normal_bindings((statement,), incoming_bindings)
    conflicts: set[str] = set()
    for method in node.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        method_receiver = _instance_receiver(method, method_bindings[method])
        if method_receiver is None:
            continue
        visitor = _ReceiverWrites(method_receiver)
        for statement in method.body:
            visitor.visit(statement)
        conflicts.update(write.attr for write in visitor.writes if proven.get(write.attr) is not write)
    return _ClassCache(
        {name: domain for name, domain in selectors.items() if name not in conflicts},
        {name: domain for name, domain in engines.items() if name not in conflicts},
    )


def _stable_module_bindings(node: ast.Module) -> dict[str, str]:
    """Deferred globals only: canonical imports without conflicting binders."""
    final = _normal_bindings(node.body, dict(_BUILTINS))
    nonimports, _ = possibly_bound_names(node.body, resolve_import=lambda _: ImportAliasEffect())
    imports: dict[str, set[str]] = {}
    for statement in node.body:
        for child in iter_own_scope(statement):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for name, target in import_alias_effect(child).proven:
                    imports.setdefault(name, set()).add(target)
    return {name: target for name, target in final.items() if name not in nonimports and len(imports.get(name, {target})) == 1}


class _ExpressionWrites(ast.NodeVisitor):
    """Visible enclosing walrus targets that an expression may execute."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.names.update(assignment_target_names(node.target))
        self.visit(node.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *(value for value in node.args.kw_defaults if value is not None)):
            self.visit(default)

    def generator_body(self, node: ast.GeneratorExp) -> None:
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        self.visit(node.elt)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self.visit(node.generators[0].iter)

    def visit_Starred(self, node: ast.Starred) -> None:
        self.visit(node.value)
        if isinstance(node.value, ast.GeneratorExp):
            self.generator_body(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        self.visit(node.func)
        for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
            self.visit(argument)
            if isinstance(argument, ast.GeneratorExp):
                self.generator_body(argument)


class _BindingVisitor(ast.NodeVisitor):
    """Observe calls before binders, with separate eager/deferred namespaces."""

    def __init__(self) -> None:
        self.builders: dict[ast.Call, tuple[str, str]] = {}
        self.guards: dict[Dispatch, Guard] = {}
        self.complements: dict[Dispatch, Guard] = {}
        self.eager_generators: set[ast.GeneratorExp] = set()
        self.compared: set[str] = set()
        self.facts = _Facts(dict(_BUILTINS))
        self._module_bindings: dict[str, str] = {}
        self._kind = "module"
        self._closure = _Facts()
        self._class_eager = _Facts()
        self._cache = _ClassCache({}, {})
        self._class_identity = ""
        self._deferred: set[ast.FunctionDef | ast.AsyncFunctionDef] = set()
        self._noninstance: set[ast.FunctionDef | ast.AsyncFunctionDef] = set()
        self._comprehension_targets: set[str] | None = None

    @classmethod
    def expression_facts(cls, node: ast.expr, incoming: _Facts) -> _Facts:
        """Evaluate initializer expression effects with the same grammar."""
        visitor = cls()
        visitor.facts = incoming.copy()
        visitor._kind = "function"
        visitor.visit(node)
        return visitor.facts

    def _suite(self, body: Sequence[ast.stmt]) -> None:
        factory = _returned_function(body)
        if factory is not None:
            self._deferred.add(factory)
        excluded: set[tuple[str, str]] = set()
        for statement in body:
            if isinstance(statement, ast.If):
                self._guard(statement)
                guard = self.guards.get(statement)
                if guard is not None and (guard.selector, guard.dialect) in excluded:
                    self.visit(statement.test)
                    self._suite(statement.orelse)
                    continue
            before = self.facts.copy()
            self.visit(statement)
            if isinstance(statement, ast.Return) and factory is not None:
                self.facts = before.copy()
                self._function_body(factory)
            if isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.Match)):
                self.facts = _compound_facts(statement, before)
                if isinstance(statement, ast.If):
                    _rejection(statement, self.facts)
            self.facts.bindings = _normal_bindings((statement,), before.bindings)
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.facts.invalidate((statement.name,), statement)
            if isinstance(statement, ast.If) and not statement.orelse and statement.body and isinstance(statement.body[-1], ast.Return):
                guard = self.guards.get(statement)
                if guard is not None:
                    excluded.add((guard.selector, guard.dialect))
        if factory is not None:
            self._deferred.remove(factory)

    def visit_Module(self, node: ast.Module) -> None:
        self._module_bindings = _stable_module_bindings(node)
        self._suite(node.body)

    def visit_Call(self, node: ast.Call) -> None:
        dotted = _dotted(node.func)
        if dotted is not None:
            target = _absolute_target(node.func, self.facts.bindings)
            if (
                target in _EAGER_CONSUMERS
                and node.args
                and isinstance(node.args[0], ast.GeneratorExp)
                and not any(generator.is_async for generator in node.args[0].generators)
            ):
                self.eager_generators.add(node.args[0])
            for module, dialect in ((SQLITE_MODULE, "sqlite"), (POSTGRESQL_MODULE, "postgresql")):
                if target == f"{module}.insert":
                    self.builders[node] = dotted, dialect
        self.visit(node.func)
        possible: list[ast.GeneratorExp] = []
        for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
            if isinstance(argument, ast.GeneratorExp):
                self.visit(argument.generators[0].iter)
                possible.append(argument)
            elif (
                len(node.args) == 1
                and argument is node.args[0]
                and isinstance(argument, ast.Starred)
                and isinstance(argument.value, ast.GeneratorExp)
            ):
                # A sole *generator is converted to positional arguments by
                # CALL_FUNCTION_EX after keyword evaluation. Other starred
                # positional forms expand during argument construction.
                self.visit(argument.value.generators[0].iter)
                possible.append(argument.value)
            else:
                self.visit(argument)
        writes = _ExpressionWrites()
        if len(possible) > 1:
            for generator in possible:
                writes.generator_body(generator)
        for generator in possible:
            # A callee may consume direct generators in any order. Their
            # possible effects do not grant dispatch ownership to an unknown call.
            self._comprehension(generator, visit_first_iterable=False, publish_effects=True, entry_rebinds=writes.names)

    def visit_Starred(self, node: ast.Starred) -> None:
        if isinstance(node.value, ast.GeneratorExp):
            self._comprehension(node.value, publish_effects=True)
        else:
            self.visit(node.value)

    def visit_Compare(self, node: ast.Compare) -> None:
        for left, right in pairwise((node.left, *node.comparators)):
            if self.facts.selector(left) is not None:
                self.compared.update(_literals(right) & SUPPORTED_DIALECTS)
            if self.facts.selector(right) is not None:
                self.compared.update(_literals(left) & SUPPORTED_DIALECTS)
        self.generic_visit(node)

    def _guard(self, node: Dispatch) -> None:
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
            return
        left, right = test.left, test.comparators[0]
        selector = self.facts.selector(left)
        if isinstance(test.ops[0], ast.Eq) and selector is None:
            left, right = right, left
            selector = self.facts.selector(left)
        if selector is None:
            return
        if isinstance(test.ops[0], ast.Eq):
            if not isinstance(right, ast.Constant):
                return
        elif isinstance(test.ops[0], ast.In):
            if not isinstance(right, (ast.List, ast.Tuple, ast.Set)):
                return
        else:
            return
        literals = _literals(right)
        if len(literals) != 1 or not literals <= SUPPORTED_DIALECTS:
            return
        dependencies = frozenset(child.id for child in ast.walk(left) if isinstance(child, ast.Name))
        dialect = next(iter(literals))
        self.guards[node] = Guard(selector, dependencies, dialect)
        if self.facts.domains.get(selector) == SUPPORTED_DIALECTS:
            self.complements[node] = Guard(selector, dependencies, next(iter(SUPPORTED_DIALECTS - literals)))

    def visit_If(self, node: ast.If) -> None:
        self._guard(node)
        self.visit(node.test)
        start = self.facts.copy()
        self._suite(node.body)
        self.facts = start.copy()
        self._suite(node.orelse)
        self.facts = start

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._guard(node)
        self.visit(node.test)
        start = self.facts.copy()
        self.visit(node.body)
        body = self.facts.copy()
        self.facts = start.copy()
        self.visit(node.orelse)
        self.facts = _join_expression_facts(node, (body, self.facts))

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.visit(node.values[0])
        paths = [self.facts.copy()]
        for value in node.values[1:]:
            self.visit(value)
            paths.append(self.facts.copy())
        self.facts = _join_expression_facts(node, paths)

    def _headers(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        if not isinstance(node, ast.Lambda):
            for decorator in node.decorator_list:
                if _absolute_target(decorator, self.facts.bindings) in {"builtins.staticmethod", "builtins.classmethod"}:
                    self._noninstance.add(node)
                self.visit(decorator)
            for parameter in node.type_params:
                self.visit(parameter)
        # Defaults execute before annotation expressions. This records syntax
        # sites, not a claim that postponed annotations execute at definition.
        for default in (*node.args.defaults, *(value for value in node.args.kw_defaults if value is not None)):
            self.visit(default)
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs, node.args.vararg, node.args.kwarg):
            if argument is not None and argument.annotation is not None:
                self.visit(argument.annotation)
        if not isinstance(node, ast.Lambda) and node.returns is not None:
            self.visit(node.returns)

    def _deferred_facts(self) -> _Facts:
        if self._kind == "class":
            return self._closure.copy()
        facts = self.facts.copy()
        if self._kind == "module":
            facts.bindings = dict(self._module_bindings)
        return facts

    def _function_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        saved = self.facts, self._kind, self._comprehension_targets
        self._comprehension_targets = None
        self.facts = self._deferred_facts()
        locals_ = argument_names(node.args) if isinstance(node, ast.Lambda) else function_local_binding_names(node)
        self.facts.invalidate(tuple(locals_), node)
        if self._kind == "class" and not isinstance(node, ast.Lambda) and node not in self._noninstance:
            positional = (*node.args.posonlyargs, *node.args.args)
            receiver = positional[0].arg if positional else None
            if receiver is not None:
                for attr, domain in self._cache.selectors.items():
                    origin = f"cached:{self._class_identity}:{attr}"
                    self.facts.cached[f"{receiver}.{attr}"] = origin
                    if domain is not None:
                        self.facts.domains[origin] = domain
                for attr, domain in self._cache.engines.items():
                    self.facts.engines[f"{receiver}.{attr}"] = domain
        self._kind = "function"
        if isinstance(node, ast.Lambda):
            self.visit(node.body)
        else:
            self._suite(node.body)
        self.facts, self._kind, self._comprehension_targets = saved

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._headers(node)
        if node not in self._deferred:
            self._function_body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._headers(node)
        if node not in self._deferred:
            self._function_body(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._headers(node)
        self._function_body(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in (*node.decorator_list, *node.bases, *node.keywords, *node.type_params):
            self.visit(expression)
        saved = self.facts, self._kind, self._closure, self._class_eager, self._cache, self._class_identity
        closure = self._deferred_facts()
        eager = self._class_eager.copy() if self._kind == "class" else self.facts.copy()
        self.facts, self._closure, self._class_eager = eager.copy(), closure, eager
        self._kind, self._cache, self._class_identity = "class", _class_cache(node, eager.bindings, closure.bindings), _site(node)
        self._suite(node.body)
        self.facts, self._kind, self._closure, self._class_eager, self._cache, self._class_identity = saved

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        origin = self.facts.selector(node.value)
        for target in node.targets:
            self.visit(target)
        self.facts.assign(node.targets, origin, node)

    def _import(self, node: ast.Import | ast.ImportFrom) -> None:
        effect = import_alias_effect(node)
        names = {name for name, _ in effect.proven} | set(effect.invalidated)
        if effect.clears_all:
            names.update((*self.facts.bindings, *self.facts.selectors, *(path.partition(".")[0] for path in self.facts.receivers)))
        self.facts.invalidate(tuple(names), node)

    def visit_Import(self, node: ast.Import) -> None:
        self._import(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._import(node)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self.visit(node.value)
        self.facts.target(node.name, node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            origin = self.facts.selector(node.value)
            self.visit(node.target)
            self.facts.assign((node.target,), origin, node)
        self.visit(node.annotation)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)
        self.facts.target(node.target, node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self.visit(target)
            self.facts.target(target, node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self.facts.assign((node.target,), self.facts.selector(node.value), node)
        if self._comprehension_targets is not None:
            self._comprehension_targets.update(assignment_target_names(node.target))

    def _loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        self.visit(node.test if isinstance(node, ast.While) else node.iter)
        start = self.facts.copy()
        names, star = possibly_bound_names(node.body)
        if star:
            names.update((*self.facts.bindings, *self.facts.selectors))
        self.facts.invalidate(tuple(names), node)
        _invalidate_attribute_targets(node.body, self.facts, node)
        if not isinstance(node, ast.While):
            self.facts.target(node.target, node)
        self._suite(node.body)
        self.facts = _compound_facts(node, start)
        self.facts.bindings = _normal_bindings((node,), start.bindings)
        self._suite(node.orelse)
        self.facts = start

    def visit_For(self, node: ast.For) -> None:
        self._loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._loop(node)

    def visit_While(self, node: ast.While) -> None:
        self._loop(node)

    def _with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
                self.facts.target(item.optional_vars, node)
        self._suite(node.body)

    def visit_With(self, node: ast.With) -> None:
        self._with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._with(node)

    def _try(self, node: ast.Try | ast.TryStar) -> None:
        start = self.facts.copy()
        self._suite(node.body)
        self._suite(node.orelse)
        names, star = possibly_bound_names(node.body)
        for handler in node.handlers:
            self.facts = start.copy()
            if star:
                names.update((*self.facts.bindings, *self.facts.selectors))
            self.facts.invalidate(tuple(names), node)
            _invalidate_attribute_targets(node.body, self.facts, node)
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name is not None:
                self.facts.invalidate((handler.name,), node)
            self._suite(handler.body)
        self.facts = _compound_facts(node, start)
        if node.finalbody:
            self.facts.bindings = evaluate_finally_entry_aliases(node, start.bindings)
            self._suite(node.finalbody)
        self.facts = start

    def visit_Try(self, node: ast.Try) -> None:
        self._try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._try(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        start = self.facts.copy()
        for case in node.cases:
            self.facts = start.copy()
            self.facts.invalidate(match_pattern_binding_names(case.pattern), node)
            if case.guard is not None:
                self.visit(case.guard)
            self._suite(case.body)
        self.facts = start

    def _comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        *,
        visit_first_iterable: bool = True,
        publish_effects: bool = False,
        entry_rebinds: set[str] | None = None,
    ) -> None:
        if visit_first_iterable:
            self.visit(node.generators[0].iter)
        start = self.facts.copy()
        if entry_rebinds:
            self.facts.invalidate(tuple(entry_rebinds), node)
        enclosing_targets = self._comprehension_targets
        targets: set[str] = set()
        self._comprehension_targets = targets
        exits = [start.copy()]
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)
                exits.append(self.facts.copy())
            self.facts.target(generator.target, node)
            for condition in generator.ifs:
                self.visit(condition)
                exits.append(self.facts.copy())
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        exits.append(self.facts.copy())
        self._comprehension_targets = enclosing_targets
        if not isinstance(node, ast.GeneratorExp) or node in self.eager_generators or publish_effects:
            self.facts = _comprehension_effects(node, start, exits, targets)
            if enclosing_targets is not None:
                enclosing_targets.update(targets)
        else:
            self.facts = start

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._comprehension(node)


class _OwnerVisitor(ast.NodeVisitor):
    """Distinct dispatch owners, including binary-domain complements."""

    def __init__(self, bindings: _BindingVisitor) -> None:
        self.bindings = bindings
        self.calls: list[BuilderCall] = []
        self.owner: Dispatch | None = None
        self.guard: Guard | None = None
        self.pairs: dict[ast.If, ast.If] = {}
        self.factories: set[ast.FunctionDef | ast.AsyncFunctionDef] = set()

    def _suite(self, body: Sequence[ast.stmt]) -> None:
        self._pair_factories(body)
        factory = _returned_function(body)
        if factory is not None and self.owner is not None:
            self.factories.add(factory)
        excluded: set[tuple[str, str]] = set()
        for statement in body:
            guard = self.bindings.guards.get(statement) if isinstance(statement, ast.If) else None
            if isinstance(statement, ast.If) and guard is not None and (guard.selector, guard.dialect) in excluded:
                self.visit(statement.test)
                self._suite(statement.orelse)
                continue
            self.visit(statement)
            if (
                isinstance(statement, ast.If)
                and guard is not None
                and not statement.orelse
                and statement.body
                and isinstance(statement.body[-1], ast.Return)
            ):
                excluded.add((guard.selector, guard.dialect))
        if factory is not None:
            self.factories.discard(factory)

    def _pair_factories(self, body: Sequence[ast.stmt]) -> None:
        consumed: set[ast.If] = set()
        for index, first in enumerate(body):
            if (
                not isinstance(first, ast.If)
                or first.orelse
                or first in consumed
                or not first.body
                or not isinstance(first.body[-1], ast.Return)
            ):
                continue
            guard = self.bindings.guards.get(first)
            if guard is None:
                continue
            for following in body[index + 1 :]:
                if isinstance(following, ast.If):
                    other = self.bindings.guards.get(following)
                    if other is not None and other.selector == guard.selector and other.dialect == guard.dialect:
                        if not following.orelse:
                            continue
                        break
                    if (
                        other is not None
                        and other.selector == guard.selector
                        and other.dialect != guard.dialect
                        and not following.orelse
                        and following.body
                        and isinstance(following.body[-1], ast.Return)
                        and following not in consumed
                    ):
                        self.pairs[first] = first
                        self.pairs[following] = first
                        consumed.update((first, following))
                    break
                if not isinstance(following, (ast.Expr, ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom, ast.Pass)):
                    break
                names, star = possibly_bound_names((following,))
                if (
                    star
                    or names & guard.dependencies
                    or any(call in self.bindings.builders for call in ast.walk(following) if isinstance(call, ast.Call))
                ):
                    break

    def generic_visit(self, node: ast.AST) -> None:
        for _, value in ast.iter_fields(node):
            if isinstance(value, list):
                if value and all(isinstance(child, ast.stmt) for child in value):
                    self._suite(value)
                else:
                    for child in value:
                        if isinstance(child, ast.AST):
                            self.visit(child)
            elif isinstance(value, ast.AST):
                self.visit(value)

    def visit_Module(self, node: ast.Module) -> None:
        self._suite(node.body)

    def visit_Call(self, node: ast.Call) -> None:
        builder = self.bindings.builders.get(node)
        if builder is not None:
            self.calls.append(BuilderCall(node, *builder, self.owner, self.guard))
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        if node not in self.bindings.guards:
            self.visit(node.test)
            self._suite(node.body)
            self._suite(node.orelse)
            return
        saved = self.owner, self.guard
        owner = self.pairs.get(node, node)
        arm = node
        while True:
            self.owner, self.guard = saved
            self.visit(arm.test)
            self.owner, self.guard = owner, self.bindings.guards.get(arm)
            self._suite(arm.body)
            if len(arm.orelse) == 1 and isinstance(arm.orelse[0], ast.If) and arm.orelse[0] in self.bindings.guards:
                arm = arm.orelse[0]
            else:
                self.owner, self.guard = owner, self.bindings.complements.get(arm)
                self._suite(arm.orelse)
                break
        self.owner, self.guard = saved

    def visit_IfExp(self, node: ast.IfExp) -> None:
        if node not in self.bindings.guards:
            self.visit(node.test)
            self.visit(node.body)
            self.visit(node.orelse)
            return
        saved = self.owner, self.guard
        self.visit(node.test)
        self.owner, self.guard = node, self.bindings.guards.get(node)
        self.visit(node.body)
        self.owner, self.guard = node, self.bindings.complements.get(node)
        self.visit(node.orelse)
        self.owner, self.guard = saved

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for expression in (*node.decorator_list, *node.type_params):
            self.visit(expression)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        saved = self.owner, self.guard
        if node not in self.factories:
            self.owner, self.guard = None, None
        self._suite(node.body)
        self.owner, self.guard = saved

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in (*node.decorator_list, *node.bases, *node.keywords, *node.type_params):
            self.visit(expression)
        self._suite(node.body)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)
        saved = self.owner, self.guard
        self.owner, self.guard = None, None
        self.visit(node.body)
        self.owner, self.guard = saved

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self.visit(node.generators[0].iter)
        saved = self.owner, self.guard
        if node not in self.bindings.eager_generators:
            self.owner, self.guard = None, None
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        self.visit(node.elt)
        self.owner, self.guard = saved


def analyze_builders(tree: ast.AST) -> BuilderAnalysis:
    """Resolve identities first, then associate dispatch ownership."""
    bindings = _BindingVisitor()
    bindings.visit(tree)
    owners = _OwnerVisitor(bindings)
    owners.visit(tree)
    return BuilderAnalysis(tuple(owners.calls), frozenset(bindings.compared))
