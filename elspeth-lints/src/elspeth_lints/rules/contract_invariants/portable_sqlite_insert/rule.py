"""Portable SQLite insert-builder rule implementation."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from elspeth_lints.core.protocols import Finding, RuleContext, RuleMetadata, RuleScope
from elspeth_lints.rules.contract_invariants.portable_sqlite_insert.analysis import (
    POSTGRESQL_MODULE,
    SQLITE_MODULE,
    SUPPORTED_DIALECTS,
    BuilderCall,
    analyze_builders,
)
from elspeth_lints.rules.contract_invariants.portable_sqlite_insert.metadata import RULE_ID, RULE_METADATA, SUGGESTION


@dataclass(frozen=True, slots=True)
class PortableSQLiteInsertRule:
    """Reject SQLite insert usage without an explicit PostgreSQL dispatch."""

    id: str = RULE_ID
    scope: RuleScope = RuleScope.INCREMENTAL
    metadata: RuleMetadata = RULE_METADATA

    def analyze(self, tree: ast.AST, file_path: Path, context: RuleContext) -> list[Finding]:
        """Analyze one Python module for SQLite-only insert builders."""
        return find_portable_sqlite_insert_findings(tree, _display_path(file_path, context.root))


def find_portable_sqlite_insert_findings(tree: ast.AST, file_path: str) -> list[Finding]:
    """Require each known SQLite call's own explicit PostgreSQL dispatch.

    The historical strict import diagnostic remains a module-level floor.
    Passing that floor does not authorize an unrelated writer. Within a
    dispatch, wrong-builder findings take precedence over consequential
    missing-counterpart noise for that same selector and owner.
    """
    analysis = analyze_builders(tree)
    sqlite_imports = _insert_imports(tree, module=SQLITE_MODULE)
    sqlite_calls = [call for call in analysis.calls if call.dialect == "sqlite"]
    if not sqlite_imports and not sqlite_calls:
        return []
    postgresql_imports = _insert_imports(tree, module=POSTGRESQL_MODULE)
    postgresql_calls = [call for call in analysis.calls if call.dialect == "postgresql"]
    if (not postgresql_imports and not postgresql_calls) or not SUPPORTED_DIALECTS.issubset(analysis.compared_dialects):
        findings = [
            _node_finding(
                file_path=file_path,
                node=node,
                message="SQLite-specific insert import has no complete PostgreSQL counterpart and explicit sqlite/postgresql dialect dispatch in this module.",
            )
            for node in sqlite_imports
        ]
        findings.extend(
            _node_finding(
                file_path=file_path,
                node=call.node,
                message="SQLite-specific insert builder call has no complete PostgreSQL counterpart and explicit sqlite/postgresql dialect dispatch in this module.",
            )
            for call in sqlite_calls
            if isinstance(call.node.func, ast.Attribute)
        )
        return sorted(findings, key=lambda finding: (finding.line, finding.column))

    wrong = [call for call in analysis.calls if call.guard is not None and call.dialect != call.guard.dialect]
    wrong_owners = {(call.owner, call.guard.selector) for call in wrong if call.guard is not None}
    paired = {
        (call.owner, call.guard.selector)
        for call in postgresql_calls
        if call.owner is not None and call.guard is not None and call.guard.dialect == "postgresql"
    }
    findings = [_misbound_branch_finding(file_path, call) for call in wrong]
    for call in sqlite_calls:
        if call in wrong:
            continue
        if call.owner is not None and call.guard is not None and call.guard.dialect == "sqlite":
            key = call.owner, call.guard.selector
            if key in paired or key in wrong_owners:
                continue
        findings.append(_unpaired_call_finding(file_path, call.node))
    return sorted(findings, key=lambda finding: (finding.line, finding.column))


def _insert_imports(tree: ast.AST, *, module: str) -> list[ast.ImportFrom]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module and any(alias.name == "insert" for alias in node.names)
    ]


def _unpaired_call_finding(file_path: str, node: ast.Call) -> Finding:
    return _node_finding(
        file_path=file_path,
        node=node,
        message=(
            "SQLite-specific insert builder call has no complete PostgreSQL counterpart and explicit "
            "sqlite/postgresql dialect dispatch owned by this writer."
        ),
    )


def _misbound_branch_finding(file_path: str, call: BuilderCall) -> Finding:
    if call.guard is None:
        raise AssertionError("Misbound calls require an explicit selecting guard")
    return _node_finding(
        file_path=file_path,
        node=call.node,
        message=(
            f"Dialect branch for {call.guard.dialect!r} invokes the {call.dialect} insert builder "
            f"{call.name!r}; bind each dialect branch to the insert builder it actually executes."
        ),
    )


def _node_finding(*, file_path: str, node: ast.ImportFrom | ast.Call, message: str) -> Finding:
    lineno = node.lineno
    col_offset = node.col_offset
    payload = f"{RULE_ID}|{file_path}|{lineno}|{col_offset}|{ast.dump(node, include_attributes=False)}"
    return Finding(
        rule_id=RULE_ID,
        file_path=file_path,
        line=lineno,
        column=col_offset,
        message=message,
        fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        severity=RULE_METADATA.severity,
        suggestion=SUGGESTION,
    )


def _display_path(file_path: Path, root: Path) -> str:
    try:
        return file_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return file_path.as_posix()


RULE = PortableSQLiteInsertRule()
