"""Shared fail-closed import-alias semantics for boundary-marker analyzers."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from elspeth_lints.core.ast_walker import iter_own_scope

_TRUST_BOUNDARY_EXPORT = "elspeth.contracts.trust_boundary"
_TRUST_BOUNDARY_FUNCTION = "elspeth.contracts.trust_boundary.trust_boundary"
_OBSERVATION_BOUNDARY_FUNCTION = "elspeth.contracts.trust_boundary.observation_boundary"

type BoundaryDecoratorKind = Literal["trust", "observation"]
type AliasTransferKind = Literal["break", "continue", "return", "raise"]


@dataclass(frozen=True, slots=True)
class ImportAliasEffect:
    """Bindings an import proves, invalidates, or may replace wholesale."""

    proven: tuple[tuple[str, str], ...] = ()
    invalidated: tuple[str, ...] = ()
    clears_all: bool = False


@dataclass(frozen=True, slots=True)
class AliasFlowPath:
    """One reachable alias state and its pending control transfer, if any."""

    aliases: dict[str, str]
    transfer: AliasTransferKind | None = None


def _dotted_name(expr: ast.expr) -> tuple[str, ...] | None:
    if isinstance(expr, ast.Name):
        return (expr.id,)
    if isinstance(expr, ast.Attribute):
        base = _dotted_name(expr.value)
        if base is not None:
            return (*base, expr.attr)
    return None


def resolve_boundary_kind(func: ast.expr, import_aliases: Mapping[str, str]) -> BoundaryDecoratorKind | None:
    """Resolve a marker only when its root has a proven canonical import."""
    parts = _dotted_name(func)
    if parts is None:
        return None
    alias_target = import_aliases.get(parts[0])
    if alias_target is None:
        return None

    resolved = ".".join((alias_target, *parts[1:]))
    if resolved == _OBSERVATION_BOUNDARY_FUNCTION:
        return "observation"
    if resolved in {_TRUST_BOUNDARY_EXPORT, _TRUST_BOUNDARY_FUNCTION}:
        return "trust"
    return None


def import_alias_effect(node: ast.Import | ast.ImportFrom) -> ImportAliasEffect:
    """Return the exact alias effect of an import statement.

    Relative imports cannot prove an absolute ELSPETH identity. They still
    bind their local names, so those names invalidate any previous trusted
    meaning. Star imports may replace every visible name and therefore clear
    the trusted alias map.
    """
    if isinstance(node, ast.Import):
        proven: list[tuple[str, str]] = []
        for alias in node.names:
            root_name = alias.name.split(".", 1)[0]
            proven.append((alias.asname or root_name, alias.name if alias.asname else root_name))
        return ImportAliasEffect(proven=tuple(proven))

    if any(alias.name == "*" for alias in node.names):
        return ImportAliasEffect(clears_all=True)
    if node.level != 0 or node.module is None:
        return ImportAliasEffect(invalidated=tuple(alias.asname or alias.name for alias in node.names))
    return ImportAliasEffect(proven=tuple((alias.asname or alias.name, f"{node.module}.{alias.name}") for alias in node.names))


def assignment_target_names(target: ast.expr) -> tuple[str, ...]:
    """Return local names bound by an assignment target."""
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, (ast.Tuple, ast.List)):
        return tuple(name for element in target.elts for name in assignment_target_names(element))
    if isinstance(target, ast.Starred):
        return assignment_target_names(target.value)
    return ()


def match_pattern_binding_names(pattern: ast.pattern) -> tuple[str, ...]:
    """Return every capture from Python's closed structural-pattern AST."""
    names: set[str] = set()

    def collect(candidate: ast.pattern) -> None:
        if isinstance(candidate, ast.MatchAs):
            if candidate.pattern is not None:
                collect(candidate.pattern)
            if candidate.name is not None:
                names.add(candidate.name)
        elif isinstance(candidate, ast.MatchStar):
            if candidate.name is not None:
                names.add(candidate.name)
        elif isinstance(candidate, ast.MatchMapping):
            for nested in candidate.patterns:
                collect(nested)
            if candidate.rest is not None:
                names.add(candidate.rest)
        elif isinstance(candidate, ast.MatchSequence):
            for nested in candidate.patterns:
                collect(nested)
        elif isinstance(candidate, ast.MatchClass):
            for nested in (*candidate.patterns, *candidate.kwd_patterns):
                collect(nested)
        elif isinstance(candidate, ast.MatchOr):
            for nested in candidate.patterns:
                collect(nested)

    collect(pattern)
    return tuple(sorted(names))


def argument_names(arguments: ast.arguments) -> set[str]:
    """Return every parameter name bound by a function or lambda."""
    names = {argument.arg for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def function_local_binding_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Return names Python treats as local throughout a function body."""
    names = argument_names(node.args)
    declarations: set[str] = set()
    own_scope_nodes = [child for statement in node.body for child in iter_own_scope(statement)]
    for child in own_scope_nodes:
        if isinstance(child, (ast.Global, ast.Nonlocal)):
            declarations.update(child.names)
        elif isinstance(child, (ast.Assign, ast.Delete)):
            for target in child.targets:
                names.update(assignment_target_names(target))
        elif isinstance(child, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.For, ast.AsyncFor)):
            names.update(assignment_target_names(child.target))
        elif isinstance(child, (ast.With, ast.AsyncWith)):
            for item in child.items:
                if item.optional_vars is not None:
                    names.update(assignment_target_names(item.optional_vars))
        elif isinstance(child, ast.ExceptHandler) and child.name is not None:
            names.add(child.name)
        elif isinstance(child, ast.Import):
            names.update(name for name, _target in import_alias_effect(child).proven)
        elif isinstance(child, ast.ImportFrom):
            effect = import_alias_effect(child)
            names.update(name for name, _target in effect.proven)
            names.update(effect.invalidated)
        elif isinstance(child, ast.Match):
            for case in child.cases:
                names.update(match_pattern_binding_names(case.pattern))
        elif isinstance(child, ast.TypeAlias):
            names.update(assignment_target_names(child.name))
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
    return names - declarations


def identical_alias_join(paths: Sequence[Mapping[str, str]]) -> dict[str, str]:
    """Keep aliases carrying the same proven target on every possible path."""
    if not paths:
        return {}
    first = paths[0]
    return {name: target for name, target in first.items() if all(name in path and path[name] == target for path in paths[1:])}


def evaluate_alias_flow(
    statements: Sequence[ast.stmt],
    import_aliases: Mapping[str, str],
) -> tuple[AliasFlowPath, ...]:
    """Evaluate import-alias effects without emitting findings or matches.

    The result retains distinct normal and abrupt paths.  Statement sequences
    stop on ``break``, ``continue``, ``return``, and ``raise``; a ``finally``
    suite is evaluated once for every incoming path, with an abrupt finalbody
    result replacing the pending transfer and normal completion preserving it.
    """
    evaluator = _AliasEffectsEvaluator()
    return evaluator.statements(statements, (AliasFlowPath(dict(import_aliases)),))


def evaluate_finally_entry_aliases(
    node: ast.Try | ast.TryStar,
    import_aliases: Mapping[str, str],
) -> dict[str, str]:
    """Return aliases proven identical on every path entering ``finally``.

    Primary AST walkers use this non-emitting replay before visiting a
    ``finally`` suite.  Their lexical visit of the try body intentionally
    continues past abrupt statements so every source site is reported once;
    that lexical state must not leak into the runtime entry state of
    ``finally``.
    """
    evaluator = _AliasEffectsEvaluator()
    paths = evaluator.try_outgoing(node, dict(import_aliases))
    return identical_alias_join(tuple(path.aliases for path in paths))


class _AliasEffectsEvaluator:
    """Non-emitting, fail-closed alias/control-flow evaluator."""

    def statements(
        self,
        statements: Sequence[ast.stmt],
        paths: Sequence[AliasFlowPath],
    ) -> tuple[AliasFlowPath, ...]:
        current = tuple(paths)
        for statement in statements:
            next_paths: list[AliasFlowPath] = []
            for path in current:
                if path.transfer is not None:
                    next_paths.append(path)
                else:
                    next_paths.extend(self.statement(statement, path.aliases))
            current = self._deduplicate(next_paths)
        return current

    def statements_with_implicit_exceptions(
        self,
        statements: Sequence[ast.stmt],
        paths: Sequence[AliasFlowPath],
    ) -> tuple[AliasFlowPath, ...]:
        """Evaluate a suite while retaining failure before each statement completes."""
        current = tuple(paths)
        for statement in statements:
            next_paths: list[AliasFlowPath] = []
            for path in current:
                if path.transfer is not None:
                    next_paths.append(path)
                    continue
                bound_names, clears_all = self._possibly_bound_names((statement,))
                exception_aliases = {} if clears_all else {name: target for name, target in path.aliases.items() if name not in bound_names}
                next_paths.append(AliasFlowPath(exception_aliases, "raise"))
                next_paths.extend(self.statement(statement, path.aliases))
            current = self._deduplicate(next_paths)
        return current

    def statement(self, node: ast.stmt, incoming: Mapping[str, str]) -> tuple[AliasFlowPath, ...]:
        aliases = dict(incoming)

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            self._apply_import(aliases, node)
            return (AliasFlowPath(aliases),)
        if isinstance(node, ast.Assign):
            self._expression(node.value, aliases)
            for target in node.targets:
                self._invalidate(aliases, assignment_target_names(target))
            return (AliasFlowPath(aliases),)
        if isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self._expression(node.value, aliases)
            self._expression(node.annotation, aliases)
            if node.value is not None:
                self._invalidate(aliases, assignment_target_names(node.target))
            return (AliasFlowPath(aliases),)
        if isinstance(node, ast.AugAssign):
            self._expression(node.target, aliases)
            self._expression(node.value, aliases)
            self._invalidate(aliases, assignment_target_names(node.target))
            return (AliasFlowPath(aliases),)
        if isinstance(node, ast.Delete):
            for target in node.targets:
                self._expression(target, aliases)
                self._invalidate(aliases, assignment_target_names(target))
            return (AliasFlowPath(aliases),)
        if isinstance(node, ast.TypeAlias):
            self._expression(node.value, aliases)
            self._invalidate(aliases, assignment_target_names(node.name))
            return (AliasFlowPath(aliases),)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._function_header(node, aliases)
            self._invalidate(aliases, (node.name,))
            return (AliasFlowPath(aliases),)
        if isinstance(node, ast.ClassDef):
            for expression in (*node.decorator_list, *node.bases):
                self._expression(expression, aliases)
            for keyword in node.keywords:
                self._expression(keyword.value, aliases)
            self._invalidate(aliases, (node.name,))
            return (AliasFlowPath(aliases),)
        if isinstance(node, ast.If):
            self._expression(node.test, aliases)
            body = self.statements(node.body, (AliasFlowPath(dict(aliases)),))
            orelse = self.statements(node.orelse, (AliasFlowPath(dict(aliases)),)) if node.orelse else (AliasFlowPath(aliases),)
            return self._deduplicate((*body, *orelse))
        if isinstance(node, ast.Match):
            self._expression(node.subject, aliases)
            paths: list[AliasFlowPath] = [AliasFlowPath(dict(aliases))]
            for case in node.cases:
                case_aliases = dict(aliases)
                self._invalidate(case_aliases, match_pattern_binding_names(case.pattern))
                if case.guard is not None:
                    self._expression(case.guard, case_aliases)
                paths.extend(self.statements(case.body, (AliasFlowPath(case_aliases),)))
            return self._deduplicate(paths)
        if isinstance(node, (ast.Try, ast.TryStar)):
            return self._try(node, aliases)
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self._expression(node.iter, aliases)
            body_entry = dict(aliases)
            self._invalidate(body_entry, assignment_target_names(node.target))
            return self._loop(node.body, node.orelse, aliases, body_entry)
        if isinstance(node, ast.While):
            self._expression(node.test, aliases)
            return self._loop(node.body, node.orelse, aliases, dict(aliases))
        if isinstance(node, (ast.With, ast.AsyncWith)):
            return self._with(node.items, node.body, aliases)
        if isinstance(node, ast.Return):
            if node.value is not None:
                self._expression(node.value, aliases)
            return (AliasFlowPath(aliases, "return"),)
        if isinstance(node, ast.Raise):
            if node.exc is not None:
                self._expression(node.exc, aliases)
            if node.cause is not None:
                self._expression(node.cause, aliases)
            return (AliasFlowPath(aliases, "raise"),)
        if isinstance(node, ast.Break):
            return (AliasFlowPath(aliases, "break"),)
        if isinstance(node, ast.Continue):
            return (AliasFlowPath(aliases, "continue"),)
        if isinstance(node, ast.Expr):
            self._expression(node.value, aliases)
            return (AliasFlowPath(aliases),)
        if isinstance(node, ast.Assert):
            self._expression(node.test, aliases)
            if node.msg is not None:
                self._expression(node.msg, aliases)
            return (AliasFlowPath(aliases),)
        return (AliasFlowPath(aliases),)

    def try_outgoing(self, node: ast.Try | ast.TryStar, aliases: dict[str, str]) -> tuple[AliasFlowPath, ...]:
        """Return reachable paths immediately before a try's ``finally`` suite."""
        body_paths = self.statements_with_implicit_exceptions(node.body, (AliasFlowPath(dict(aliases)),))
        outgoing: list[AliasFlowPath] = []
        for path in body_paths:
            if path.transfer is None and node.orelse:
                outgoing.extend(self.statements_with_implicit_exceptions(node.orelse, (AliasFlowPath(dict(path.aliases)),)))
            else:
                outgoing.append(path)

        if node.handlers:
            raised_paths = [path.aliases for path in body_paths if path.transfer == "raise"]
            handler_entry = identical_alias_join(raised_paths)
            for handler in node.handlers:
                handler_aliases = dict(handler_entry)
                if handler.type is not None:
                    self._expression(handler.type, handler_aliases)
                if handler.name is not None:
                    self._invalidate(handler_aliases, (handler.name,))
                outgoing.extend(self.statements_with_implicit_exceptions(handler.body, (AliasFlowPath(handler_aliases),)))

        return self._deduplicate(outgoing)

    def _try(self, node: ast.Try | ast.TryStar, aliases: dict[str, str]) -> tuple[AliasFlowPath, ...]:
        outgoing = self.try_outgoing(node, aliases)

        if not node.finalbody:
            return outgoing

        finalized: list[AliasFlowPath] = []
        for incoming in outgoing:
            final_paths = self.statements_with_implicit_exceptions(
                node.finalbody,
                (AliasFlowPath(dict(incoming.aliases)),),
            )
            for final_path in final_paths:
                transfer = incoming.transfer if final_path.transfer is None else final_path.transfer
                finalized.append(AliasFlowPath(dict(final_path.aliases), transfer))
        return self._deduplicate(finalized)

    def _with(
        self,
        items: Sequence[ast.withitem],
        body: Sequence[ast.stmt],
        aliases: dict[str, str],
    ) -> tuple[AliasFlowPath, ...]:
        """Model nested context managers, including possible exception suppression."""

        def enter_item(index: int, incoming: dict[str, str]) -> tuple[AliasFlowPath, ...]:
            item = items[index]
            acquired = dict(incoming)
            self._expression(item.context_expr, acquired)

            # Evaluation/entry may fail before this manager is active.  An
            # already-entered outer manager can still suppress that failure.
            paths: list[AliasFlowPath] = [AliasFlowPath(dict(acquired), "raise")]
            entered = dict(acquired)
            if item.optional_vars is not None:
                self._invalidate(entered, assignment_target_names(item.optional_vars))

            if index + 1 < len(items):
                inner_paths = enter_item(index + 1, entered)
            else:
                inner_paths = self.statements_with_implicit_exceptions(
                    body,
                    (AliasFlowPath(entered),),
                )

            for inner in inner_paths:
                paths.append(inner)
                if inner.transfer == "raise":
                    paths.append(AliasFlowPath(dict(inner.aliases)))
                # __exit__/__aexit__ may itself raise.  This manager cannot
                # suppress its own exit failure, but an outer manager can.
                paths.append(AliasFlowPath(dict(inner.aliases), "raise"))
            return self._deduplicate(paths)

        return enter_item(0, aliases)

    def _loop(
        self,
        body: Sequence[ast.stmt],
        orelse: Sequence[ast.stmt],
        zero_iteration: Mapping[str, str],
        body_entry: Mapping[str, str],
    ) -> tuple[AliasFlowPath, ...]:
        body_paths = self.statements(body, (AliasFlowPath(dict(body_entry)),))
        normal_inputs = [AliasFlowPath(dict(zero_iteration))]
        break_paths: list[AliasFlowPath] = []
        propagated: list[AliasFlowPath] = []
        for path in body_paths:
            if path.transfer in {None, "continue"}:
                normal_inputs.append(AliasFlowPath(dict(path.aliases)))
            elif path.transfer == "break":
                break_paths.append(AliasFlowPath(dict(path.aliases)))
            else:
                propagated.append(path)

        normal_paths: list[AliasFlowPath] = []
        for path in normal_inputs:
            if orelse:
                normal_paths.extend(self.statements(orelse, (path,)))
            else:
                normal_paths.append(path)
        return self._deduplicate((*normal_paths, *break_paths, *propagated))

    def _expression(self, node: ast.AST, aliases: dict[str, str]) -> None:
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, ast.NamedExpr):
            self._expression(node.value, aliases)
            self._invalidate(aliases, assignment_target_names(node.target))
            return
        for child in ast.iter_child_nodes(node):
            self._expression(child, aliases)

    def _function_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef, aliases: dict[str, str]) -> None:
        for decorator in node.decorator_list:
            self._expression(decorator, aliases)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self._expression(default, aliases)
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if argument.annotation is not None:
                self._expression(argument.annotation, aliases)
        if node.args.vararg is not None and node.args.vararg.annotation is not None:
            self._expression(node.args.vararg.annotation, aliases)
        if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
            self._expression(node.args.kwarg.annotation, aliases)
        if node.returns is not None:
            self._expression(node.returns, aliases)

    @staticmethod
    def _invalidate(aliases: dict[str, str], names: Sequence[str]) -> None:
        for name in names:
            aliases.pop(name, None)

    def _apply_import(self, aliases: dict[str, str], node: ast.Import | ast.ImportFrom) -> None:
        effect = import_alias_effect(node)
        if effect.clears_all:
            aliases.clear()
        self._invalidate(aliases, effect.invalidated)
        aliases.update(effect.proven)

    @staticmethod
    def _possibly_bound_names(statements: Sequence[ast.stmt]) -> tuple[set[str], bool]:
        names: set[str] = set()
        clears_all = False
        for statement in statements:
            for child in iter_own_scope(statement):
                if isinstance(child, (ast.Assign, ast.Delete)):
                    for target in child.targets:
                        names.update(assignment_target_names(target))
                elif isinstance(child, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.For, ast.AsyncFor)):
                    names.update(assignment_target_names(child.target))
                elif isinstance(child, (ast.With, ast.AsyncWith)):
                    for item in child.items:
                        if item.optional_vars is not None:
                            names.update(assignment_target_names(item.optional_vars))
                elif isinstance(child, ast.ExceptHandler) and child.name is not None:
                    names.add(child.name)
                elif isinstance(child, (ast.Import, ast.ImportFrom)):
                    effect = import_alias_effect(child)
                    names.update(name for name, _target in effect.proven)
                    names.update(effect.invalidated)
                    clears_all = clears_all or effect.clears_all
                elif isinstance(child, ast.Match):
                    for case in child.cases:
                        names.update(match_pattern_binding_names(case.pattern))
                elif isinstance(child, ast.TypeAlias):
                    names.update(assignment_target_names(child.name))
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(child.name)
        return names, clears_all

    @staticmethod
    def _deduplicate(paths: Sequence[AliasFlowPath]) -> tuple[AliasFlowPath, ...]:
        grouped: dict[AliasTransferKind | None, list[Mapping[str, str]]] = {}
        for path in paths:
            grouped.setdefault(path.transfer, []).append(path.aliases)
        return tuple(AliasFlowPath(identical_alias_join(group), transfer) for transfer, group in grouped.items())
