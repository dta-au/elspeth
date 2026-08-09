"""Shared AST helpers for ``@trust_boundary`` honesty-gate rules.

These helpers duplicate the decorator-recognition shape already used by
``trust_tier.tier_model.trust_boundary_suppress`` deliberately: the honesty
gates must remain independent of the suppression walk so a refactor in either
direction cannot accidentally couple the two. The recognition shape itself is
small and stable (matches the runtime decorator's import surface in
``src/elspeth/contracts/trust_boundary.py``).

Recognised decorator spellings (matching the runtime import surface):

* ``@trust_boundary(...)`` after ``from elspeth.contracts.trust_boundary import trust_boundary``;
* ``@elspeth.contracts.trust_boundary(...)`` (fully qualified attribute chain);
* ``@contracts.trust_boundary(...)`` (shortened attribute chain).
* ``@tb_mod.trust_boundary(...)`` after importing the decorator module as an alias.

Bare-decorator usage (``@trust_boundary`` without a call) is not recognised:
the runtime decorator is keyword-only and never appears without arguments.

Async functions decorated with ``@trust_boundary`` are recognised
identically — the decorator composes with any callable. Both
:class:`ast.FunctionDef` and :class:`ast.AsyncFunctionDef` are yielded by
:func:`iter_trust_boundary_decorators`.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from elspeth_lints.core.allowlist import Allowlist, AllowlistEntry, FindingKey, load_allowlist, verify_entry_binding_against_finding
from elspeth_lints.core.allowlist_governance import allowlist_governance_findings
from elspeth_lints.core.boundary_aliases import (
    BoundaryDecoratorKind,
    argument_names,
    assignment_target_names,
    evaluate_alias_flow,
    evaluate_finally_entry_aliases,
    function_local_binding_names,
    identical_alias_join,
    import_alias_effect,
    match_pattern_binding_names,
    resolve_boundary_kind,
)
from elspeth_lints.core.protocols import Finding, RuleMetadata

TRUST_BOUNDARY_ALLOWLIST_DIR = "enforce_trust_boundary_honesty"


@dataclass(frozen=True, slots=True)
class BoundaryDecoratorMatch:
    """Import-resolved ELSPETH boundary marker and decorated function."""

    function: ast.FunctionDef | ast.AsyncFunctionDef
    call: ast.Call
    kind: BoundaryDecoratorKind

    @property
    def non_raising(self) -> bool:
        return self.kind == "observation"


def _boundary_decorator(decorator: ast.expr, *, import_aliases: Mapping[str, str]) -> tuple[ast.Call, BoundaryDecoratorKind] | None:
    """Return the ``ast.Call`` if ``decorator`` references ``trust_boundary``.

    See module docstring for the recognised spellings. Returns ``None`` for any
    other decorator shape; callers iterate the full decorator list.
    """
    if not isinstance(decorator, ast.Call):
        return None
    kind = resolve_boundary_kind(decorator.func, import_aliases)
    return (decorator, kind) if kind is not None else None


class _TrustBoundaryDecoratorVisitor(ast.NodeVisitor):
    """Find Elspeth trust-boundary decorators with lexical import awareness."""

    def __init__(self) -> None:
        self.matches: list[BoundaryDecoratorMatch] = []
        self._import_alias_stack: list[tuple[str, dict[str, str]]] = [("module", {})]
        self._import_alias_mutation_stack: list[tuple[dict[str, str], set[str]]] = []

    @property
    def _import_aliases(self) -> dict[str, str]:
        return self._import_alias_stack[-1][1]

    def _push_scope(self, kind: str, *, local_bindings: Iterable[str] = ()) -> None:
        """Enter a scope using the nearest Python closure-capable frame.

        A class namespace is visible while method decorators execute, but it
        is not a closure for method bodies or nested classes. Skipping class
        frames prevents class-local imports from leaking into method bodies
        while preserving aliases from an enclosing function or module.
        """
        for frame_kind, aliases in reversed(self._import_alias_stack):
            if frame_kind != "class":
                scoped_aliases = dict(aliases)
                for name in local_bindings:
                    scoped_aliases.pop(name, None)
                self._import_alias_stack.append((kind, scoped_aliases))
                return
        raise AssertionError("module import-alias scope is always present")

    def _pop_scope(self) -> None:
        self._import_alias_stack.pop()

    def _record_alias_mutations(self, names: Iterable[str]) -> None:
        bound_names = set(names)
        aliases = self._import_aliases
        for flow_aliases, mutations in self._import_alias_mutation_stack:
            if flow_aliases is aliases:
                mutations.update(bound_names)

    def _invalidate_aliases(self, names: Iterable[str]) -> None:
        bound_names = set(names)
        self._record_alias_mutations(bound_names)
        for name in bound_names:
            self._import_aliases.pop(name, None)

    def _apply_import(self, node: ast.Import | ast.ImportFrom) -> None:
        effect = import_alias_effect(node)
        if effect.clears_all:
            self._record_alias_mutations(self._import_aliases)
            self._import_aliases.clear()
        self._invalidate_aliases(effect.invalidated)
        for name, target in effect.proven:
            self._record_alias_mutations((name,))
            self._import_aliases[name] = target

    def _restore_aliases(self, snapshot: Mapping[str, str]) -> None:
        self._import_aliases.clear()
        self._import_aliases.update(snapshot)

    def _join_alias_paths(self, paths: Sequence[Mapping[str, str]]) -> None:
        self._restore_aliases(identical_alias_join(paths))

    def _visit_statements(self, statements: Sequence[ast.stmt]) -> None:
        for statement in statements:
            self.visit(statement)

    def _visit_tracked_statements(self, statements: Sequence[ast.stmt]) -> tuple[dict[str, str], set[str]]:
        aliases = self._import_aliases
        mutations: set[str] = set()
        self._import_alias_mutation_stack.append((aliases, mutations))
        try:
            self._visit_statements(statements)
        finally:
            popped_aliases, popped_mutations = self._import_alias_mutation_stack.pop()
            if popped_aliases is not aliases or popped_mutations is not mutations:
                raise AssertionError("honesty alias mutation stack is unbalanced")
        return dict(aliases), mutations

    def visit_Import(self, node: ast.Import) -> None:
        self._apply_import(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._apply_import(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._push_scope("class")
        try:
            self._visit_statements(node.body)
        finally:
            self._pop_scope()
        self._invalidate_aliases((node.name,))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            matched = _boundary_decorator(decorator, import_aliases=self._import_aliases)
            if matched is not None:
                call, kind = matched
                self.matches.append(BoundaryDecoratorMatch(function=node, call=call, kind=kind))
                # A function should have at most one @trust_boundary; if a
                # malformed decorator stack accidentally repeats it, we still
                # yield the first only — duplicate findings would be noise.
                break

        self._push_scope("function", local_bindings=function_local_binding_names(node))
        try:
            self._visit_statements(node.body)
        finally:
            self._pop_scope()
        self._invalidate_aliases((node.name,))

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._push_scope("function", local_bindings=argument_names(node.args))
        try:
            self.visit(node.body)
        finally:
            self._pop_scope()

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        for target in node.targets:
            self._invalidate_aliases(assignment_target_names(target))

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if node.value is not None:
            self._invalidate_aliases(assignment_target_names(node.target))

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.generic_visit(node)
        self._invalidate_aliases(assignment_target_names(node.target))

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.generic_visit(node)
        self._invalidate_aliases(assignment_target_names(node.target))

    def visit_Delete(self, node: ast.Delete) -> None:
        self.generic_visit(node)
        for target in node.targets:
            self._invalidate_aliases(assignment_target_names(target))

    def _visit_loop_body(
        self,
        *,
        entry_aliases: Mapping[str, str],
        body: Sequence[ast.stmt],
        target: ast.expr | None = None,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """Visit output sites once and compute reachable loop alias paths."""
        flow_entry = dict(entry_aliases)
        if target is not None:
            for name in assignment_target_names(target):
                flow_entry.pop(name, None)
        flow_paths = evaluate_alias_flow(body, flow_entry)

        self._restore_aliases(entry_aliases)
        if target is not None:
            self._invalidate_aliases(assignment_target_names(target))
        self._visit_statements(body)

        normal_paths = [dict(path.aliases) for path in flow_paths if path.transfer in {None, "continue"}]
        break_paths = [dict(path.aliases) for path in flow_paths if path.transfer == "break"]
        return normal_paths, break_paths

    def _finish_loop(
        self,
        *,
        entry_aliases: Mapping[str, str],
        normal_paths: Sequence[Mapping[str, str]],
        break_paths: Sequence[Mapping[str, str]],
        orelse: Sequence[ast.stmt],
    ) -> None:
        """Join zero/body paths, then reachable ``else`` and break exits."""
        normal_exit = identical_alias_join((entry_aliases, *normal_paths))
        self._restore_aliases(normal_exit)
        self._visit_statements(orelse)
        orelse_paths = evaluate_alias_flow(orelse, normal_exit)
        post_loop_paths = [dict(path.aliases) for path in orelse_paths if path.transfer is None]
        self._join_alias_paths((*post_loop_paths, *break_paths))

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        entry_aliases = dict(self._import_aliases)
        normal_paths, break_paths = self._visit_loop_body(
            entry_aliases=entry_aliases,
            body=node.body,
            target=node.target,
        )
        self._finish_loop(
            entry_aliases=entry_aliases,
            normal_paths=normal_paths,
            break_paths=break_paths,
            orelse=node.orelse,
        )

    def visit_For(self, node: ast.For) -> None:
        self._visit_for(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_for(node)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        entry_aliases = dict(self._import_aliases)
        normal_paths, break_paths = self._visit_loop_body(
            entry_aliases=entry_aliases,
            body=node.body,
        )
        self._finish_loop(
            entry_aliases=entry_aliases,
            normal_paths=normal_paths,
            break_paths=break_paths,
            orelse=node.orelse,
        )

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._invalidate_aliases(assignment_target_names(item.optional_vars))
        self._visit_statements(node.body)

    def visit_With(self, node: ast.With) -> None:
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._visit_with(node)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self.generic_visit(node)
        self._invalidate_aliases(assignment_target_names(node.name))

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        start = dict(self._import_aliases)
        self._restore_aliases(start)
        self._visit_statements(node.body)
        body_aliases = dict(self._import_aliases)
        self._restore_aliases(start)
        self._visit_statements(node.orelse)
        orelse_aliases = dict(self._import_aliases) if node.orelse else start
        self._join_alias_paths((body_aliases, orelse_aliases))

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        aliases = self._import_aliases
        start = dict(aliases)
        match_mutations: set[str] = set()
        for case in node.cases:
            self._restore_aliases(start)
            case_mutations: set[str] = set()
            self._import_alias_mutation_stack.append((aliases, case_mutations))
            try:
                self._invalidate_aliases(match_pattern_binding_names(case.pattern))
                if case.guard is not None:
                    self.visit(case.guard)
                self._visit_statements(case.body)
            finally:
                popped_aliases, popped_mutations = self._import_alias_mutation_stack.pop()
                if popped_aliases is not aliases or popped_mutations is not case_mutations:
                    raise AssertionError("honesty match alias mutation stack is unbalanced")
            match_mutations.update(case_mutations)
        self._restore_aliases({name: target for name, target in start.items() if name not in match_mutations})

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        start = dict(self._import_aliases)
        finally_entry = evaluate_finally_entry_aliases(node, start) if node.finalbody else None
        self._restore_aliases(start)
        body_aliases, body_mutations = self._visit_tracked_statements(node.body)
        if node.orelse:
            self._restore_aliases(body_aliases)
            self._visit_statements(node.orelse)
            body_aliases = dict(self._import_aliases)
        endpoints = [body_aliases]

        handler_start = {name: target for name, target in start.items() if name not in body_mutations}
        for handler in node.handlers:
            self._restore_aliases(handler_start)
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name is not None:
                self._invalidate_aliases((handler.name,))
            self._visit_statements(handler.body)
            endpoints.append(dict(self._import_aliases))

        self._join_alias_paths(tuple(endpoints))
        if finally_entry is not None:
            self._restore_aliases(finally_entry)
        self._visit_statements(node.finalbody)

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._visit_try(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:
            self._invalidate_aliases((node.name,))
        self._visit_statements(node.body)


def iter_trust_boundary_decorators(
    tree: ast.AST,
) -> Iterator[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.Call]]:
    """Yield ``(function_node, decorator_call)`` for every ``@trust_boundary`` in ``tree``.

    Walks the whole tree (module, classes, nested functions). The decorator
    call yielded is the ``ast.Call`` node — callers use
    :func:`extract_keywords` to read its kwargs.

    Ordering is deterministic: statements are visited in lexical order while
    preserving import aliases visible at each decorator site.
    """
    visitor = _TrustBoundaryDecoratorVisitor()
    visitor.visit(tree)
    for match in visitor.matches:
        yield match.function, match.call


def iter_boundary_decorators(tree: ast.AST) -> Iterator[BoundaryDecoratorMatch]:
    """Yield boundary markers with their import-resolved semantic kind."""
    visitor = _TrustBoundaryDecoratorVisitor()
    visitor.visit(tree)
    yield from visitor.matches


def _literal_value(node: ast.expr) -> tuple[bool, object]:
    """Try to extract a static literal from an AST expression.

    Returns ``(ok, value)``. Allowed shapes:

    * :class:`ast.Constant` scalars (``str``, ``int``, ``bool``, ``None``,
      ``float``, ``bytes``);
    * :class:`ast.Tuple`, :class:`ast.List`, :class:`ast.Set`,
      :class:`ast.Dict` composed recursively of allowed shapes.

    Anything referencing a name, a call, an attribute, an f-string, or a
    comprehension is treated as non-literal. The honesty-gate rules need
    static metadata; an unverifiable value is a rule violation in itself —
    each honesty rule self-enforces by emitting its own
    ``R_TB_NONLITERAL`` finding on top of any duplicate finding emitted by
    ``trust_tier.tier_model``. The redundancy is deliberate per the
    honesty-gate cross-rule-bypass remediation (epic elspeth-2ed3bb0f7d,
    ticket elspeth-1f4634235a / C6-4): suppressing ``trust_tier.tier_model``
    on a file must NOT grant honesty-gate immunity.
    """
    if isinstance(node, ast.Constant):
        return True, node.value
    if isinstance(node, ast.Tuple):
        items: list[object] = []
        for elt in node.elts:
            ok, value = _literal_value(elt)
            if not ok:
                return False, None
            items.append(value)
        return True, tuple(items)
    if isinstance(node, ast.List):
        list_items: list[object] = []
        for elt in node.elts:
            ok, value = _literal_value(elt)
            if not ok:
                return False, None
            list_items.append(value)
        return True, list_items
    if isinstance(node, ast.Set):
        set_items: list[object] = []
        for elt in node.elts:
            ok, value = _literal_value(elt)
            if not ok:
                return False, None
            set_items.append(value)
        return True, set(set_items)
    if isinstance(node, ast.Dict):
        result: dict[object, object] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=False):
            if key_node is None:
                return False, None
            ok_key, key_val = _literal_value(key_node)
            if not ok_key:
                return False, None
            ok_val, val_val = _literal_value(value_node)
            if not ok_val:
                return False, None
            result[key_val] = val_val
        return True, result
    return False, None


@dataclass(frozen=True, slots=True)
class KeywordExtraction:
    """Outcome of parsing a ``@trust_boundary(...)`` call's kwargs.

    Exactly one of ``kwargs`` and ``nonliteral_message`` is non-``None``:

    * ``kwargs`` is the literal-value dict when every kwarg parsed
      successfully — the rule walks the dict as before.
    * ``nonliteral_message`` is a human-readable diagnostic when any kwarg
      cannot be statically evaluated (a name reference, a call, a
      comprehension), uses ``**kwargs``-unpacking, or the call has
      positional arguments. The caller emits this as an
      ``R_TB_NONLITERAL`` finding on the decorator call site.

    Why a tagged result rather than ``dict | None``: the previous
    ``None`` sentinel made every caller ``continue`` silently, deferring
    to the ``trust_tier.tier_model`` rule. That created a cross-rule
    bypass — suppressing ``tier_model`` on a file granted honesty-gate
    immunity for non-literal decorators on that same file. The honesty
    gates must self-enforce literal-only kwargs; see
    epic elspeth-2ed3bb0f7d, ticket elspeth-1f4634235a (C6-4). The
    redundant finding when ``tier_model`` is also active is deliberate.
    """

    kwargs: dict[str, object] | None
    nonliteral_message: str | None


def extract_keywords(call: ast.Call, *, implicit_non_raising: bool = False) -> KeywordExtraction:
    """Return the decorator's kwargs as a tagged result.

    Positional arguments are rejected (the runtime decorator is keyword-only;
    a positional-arg call is malformed). ``**kwargs``-style unpacking is
    rejected (``keyword.arg is None``). Any kwarg whose value cannot be
    literal-evaluated is also rejected. In all three rejection cases the
    returned :class:`KeywordExtraction` has ``kwargs is None`` and
    ``nonliteral_message`` populated with a human-readable diagnostic;
    callers must emit an ``R_TB_NONLITERAL`` finding rather than silently
    skipping (which would defer to ``trust_tier.tier_model`` and create a
    cross-rule bypass — see :class:`KeywordExtraction`).

    Returns:
        :class:`KeywordExtraction` — ``kwargs`` populated on success,
        ``nonliteral_message`` populated when any kwarg is non-literal, the
        call has positional args, or ``**kwargs`` unpacking is used.
    """
    if call.args:
        return KeywordExtraction(
            kwargs=None,
            nonliteral_message=(
                "@trust_boundary received positional arguments; the decorator is keyword-only and its honesty-gate metadata cannot be read."
            ),
        )
    parsed: dict[str, object] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            return KeywordExtraction(
                kwargs=None,
                nonliteral_message=(
                    "@trust_boundary uses **-unpacking for its arguments; static analysis cannot read the honesty-gate metadata."
                ),
            )
        ok, value = _literal_value(keyword.value)
        if not ok:
            return KeywordExtraction(
                kwargs=None,
                nonliteral_message=(
                    f"@trust_boundary kwarg {keyword.arg!r} is not a static "
                    "literal (e.g. it references a name, a call, an attribute, "
                    "or a comprehension); the honesty-gate metadata is "
                    "unverifiable so the decorator cannot be trusted to mark a "
                    "real trust boundary."
                ),
            )
        parsed[keyword.arg] = value
    if implicit_non_raising:
        parsed.setdefault("non_raising", True)
    return KeywordExtraction(kwargs=parsed, nonliteral_message=None)


def display_path(file_path: Path, root: Path) -> str:
    """Return the path format used by elspeth-lints rules for this scan root.

    Matches the convention in
    :func:`elspeth_lints.rules.immutability.shared.display_path`: a
    forward-slash POSIX path relative to ``root``, or the absolute path if the
    file is outside the scan root.
    """
    try:
        return file_path.relative_to(root).as_posix()
    except ValueError:
        return file_path.as_posix()


def repository_root(root: Path, explicit_repo_root: Path | None = None) -> Path:
    """Return the repository root from a scan root.

    If ``explicit_repo_root`` is provided, it wins. This lets CI use a
    non-canonical scan root while still resolving repository-relative
    evidence paths such as ``tests/...`` deterministically.

    When the scan root is ``<repo>/src/elspeth`` (the canonical invocation),
    the repository root is ``root.parent.parent``. Otherwise the scan root
    itself is the repository root. Same heuristic as
    :func:`elspeth_lints.rules.audit_evidence.shared.repo_relative_display_path`.
    The ``trust_boundary.tests`` rule needs this to resolve ``test_ref``
    pytest nodeids that point to ``tests/...`` paths even though the scan
    root is ``src/elspeth/``.
    """
    if explicit_repo_root is not None:
        return explicit_repo_root
    if root.name == "elspeth" and root.parent.name == "src":
        return root.parent.parent
    return root


def allowlist_path_for_root(root: Path) -> Path:
    """Return the shared trust-boundary honesty-gate allowlist path.

    The canonical scan root in CI is ``<repo>/src/elspeth`` while allowlist
    governance lives under ``<repo>/config/cicd``. This mirrors tier_model's
    upward discovery, but all three trust-boundary honesty gates share one
    directory so escape entries stay in one audited surface.
    """
    relative_dir = Path("config") / "cicd" / TRUST_BOUNDARY_ALLOWLIST_DIR
    local_dir = root / relative_dir
    if local_dir.is_dir():
        return local_dir

    if root.name == "elspeth" and root.parent.name == "src":
        for candidate in root.parents:
            dir_path = candidate / relative_dir
            if dir_path.is_dir():
                return dir_path
    return local_dir


def load_honesty_gate_allowlist(
    root: Path,
    *,
    allowlist_dir_override: Path | None,
    valid_rule_ids: frozenset[str],
) -> Allowlist:
    """Load the narrow allowlist surface for trust-boundary honesty gates.

    Honesty-gate findings are suppressible only as exact, judge-attested
    entries. Pre-judge grandfathering and per-file blanket rules are rejected
    here because these rules guard the decorator's own truthfulness; an escape
    must be a reviewed exception to one finding, not a new suppression language.
    """
    allowlist_path = allowlist_dir_override if allowlist_dir_override is not None else allowlist_path_for_root(root)
    if not allowlist_path.exists():
        if allowlist_dir_override is not None:
            raise FileNotFoundError(f"{allowlist_path}: trust-boundary allowlist directory does not exist")
        return Allowlist(entries=[])
    allowlist = load_allowlist(allowlist_path, valid_rule_ids=valid_rule_ids, source_root=root)
    _validate_honesty_gate_allowlist(allowlist, valid_rule_ids=valid_rule_ids)
    return allowlist


def filter_allowlisted_findings(
    findings: list[Finding],
    allowlist: Allowlist,
    *,
    allowlist_dir: Path | None = None,
    governance_emitted_dirs: set[str] | None = None,
    emit_allowlist_governance: bool = True,
) -> list[Finding]:
    """Return findings not covered by the trust-boundary allowlist."""
    active = [finding for finding in findings if _allowlist_match(allowlist, finding) is None]
    if allowlist_dir is None:
        return active
    return [
        *active,
        *allowlist_governance_findings(
            allowlist,
            allowlist_dir,
            emitted_dirs=governance_emitted_dirs,
            enabled=emit_allowlist_governance,
        ),
    ]


def _validate_honesty_gate_allowlist(allowlist: Allowlist, *, valid_rule_ids: frozenset[str]) -> None:
    if allowlist.per_file_rules:
        source_files = ", ".join(sorted({rule.source_file for rule in allowlist.per_file_rules if rule.source_file}))
        suffix = f" in {source_files}" if source_files else ""
        raise ValueError(f"trust-boundary honesty gates only allow exact allow_hits; per_file_rules are forbidden{suffix}")

    for entry in allowlist.entries:
        rule_id = _rule_id_from_canonical_key(entry.key)
        source_ctx = f" in {entry.source_file}" if entry.source_file else ""
        if rule_id not in valid_rule_ids:
            raise ValueError(f"allow_hits entry has unknown trust-boundary honesty rule id {rule_id!r}{source_ctx}: {entry.key}")
        if entry.judge_verdict is None:
            raise ValueError(f"trust-boundary honesty allow_hits entry must carry judge_verdict metadata{source_ctx}: {entry.key}")
        if entry.expires is None:
            raise ValueError(f"trust-boundary honesty allow_hits entry must expire{source_ctx}: {entry.key}")


def _allowlist_match(allowlist: Allowlist, finding: Finding) -> object | None:
    matched = allowlist.match(
        FindingKey(
            file_path=finding.file_path,
            rule_id=finding.rule_id,
            symbol_context=finding.symbol_context,
            fingerprint=finding.fingerprint,
        )
    )
    if isinstance(matched, AllowlistEntry) and _is_expired_entry(matched):
        matched.matched = False
        return None
    if isinstance(matched, AllowlistEntry) and matched.judge_verdict is not None:
        if not finding.ast_path:
            raise ValueError(
                f"trust-boundary finding {finding.canonical_key()} has no ast_path; "
                "signed allowlist entries require binding to the inspected AST node."
            )
        # Trust-boundary findings carry the shared protocol's empty, unstamped
        # scope_fingerprint. They are v1-only today (no signed v2 trust_boundary entries on disk), so
        # the v1 branch ignores this value. A FUTURE v2 trust_boundary entry would hit the
        # "scope_fingerprint missing on the live finding" crash above — correct fail-closed
        # behaviour and a clear signal to wire trust_boundary's scanner to stamp the field.
        # Do NOT fabricate a value; the empty string is the honest "not computed" signal.
        verify_entry_binding_against_finding(
            matched,
            file_path=finding.file_path,
            ast_path=finding.ast_path,
            scope_fingerprint=finding.scope_fingerprint,
        )
    return matched


def _is_expired_entry(entry: AllowlistEntry) -> bool:
    return entry.expires is not None and entry.expires < datetime.now(UTC).date()


def _rule_id_from_canonical_key(key: str) -> str:
    marker = ".py:"
    py_index = key.find(marker)
    if py_index < 0 or ":fp=" not in key:
        raise ValueError(f"allowlist key is not in canonical finding form: {key!r}")
    remainder = key[py_index + len(marker) :]
    rule_id, sep, _symbol_and_fingerprint = remainder.partition(":")
    if not sep or not rule_id:
        raise ValueError(f"allowlist key is missing its rule-id segment: {key!r}")
    return rule_id


def make_decorator_finding(
    *,
    metadata: RuleMetadata,
    rule_id: str,
    file_path: str,
    call: ast.Call,
    message: str,
    suggestion: str,
    symbol_context: tuple[str, ...] = (),
) -> Finding:
    """Build a finding for a ``@trust_boundary`` decorator call site.

    All trust-boundary honesty gates fingerprint decorator findings with
    the same stable payload: ``rule_id|file_path|lineno|col``. Keeping this
    helper in ``shared`` prevents the three rules from drifting if the
    fingerprint shape changes again.
    """
    fingerprint = hashlib.sha256(f"{rule_id}|{file_path}|{call.lineno}|{call.col_offset}".encode()).hexdigest()[:16]
    return Finding(
        rule_id=rule_id,
        file_path=file_path,
        line=call.lineno,
        column=call.col_offset,
        message=message,
        fingerprint=fingerprint,
        severity=metadata.severity,
        suggestion=suggestion,
        symbol_context=symbol_context,
        ast_path=f"decorator:{call.lineno}:{call.col_offset}",
    )
