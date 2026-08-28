"""ADR-019 tests-to-source mapping inventory rule implementation."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path

from elspeth_lints.core.allowlist import Allowlist, FindingKey, load_allowlist
from elspeth_lints.core.allowlist_governance import allowlist_governance_findings_for_root
from elspeth_lints.core.ast_walker import iter_python_files
from elspeth_lints.core.protocols import Finding as LintFinding
from elspeth_lints.core.protocols import RuleContext, RuleMetadata, RuleScope, Severity
from elspeth_lints.rules.manifest.test_to_source_mapping.metadata import RULE_ID, RULE_METADATA


class FindingKind(StrEnum):
    """ADR-019 tests-tree inventory finding kinds."""

    ROW_OUTCOME_ATTRIBUTE = "row_outcome_attribute"
    ROW_OUTCOME_COMPARE = "row_outcome_compare"
    ROW_OUTCOME_COLLECTION = "row_outcome_collection"
    ROW_OUTCOME_MEMBERSHIP = "row_outcome_membership"
    OLD_OUTCOME_STRING_COMPARE = "old_outcome_string_compare"
    OLD_OUTCOME_STRING_MEMBERSHIP = "old_outcome_string_membership"
    RAW_TOKEN_OUTCOMES_SQL = "raw_token_outcomes_sql"
    TOKEN_OUTCOMES_SCHEMA_READ = "token_outcomes_schema_read"


ROW_OUTCOME_VALUES = frozenset(
    {
        "completed",
        "routed",
        "routed_on_error",
        "forked",
        "failed",
        "quarantined",
        "diverted",
        "consumed_in_batch",
        "dropped_by_filter",
        "coalesced",
        "expanded",
        "buffered",
    }
)

# A SQL statement verb must lead the string, and a clause keyword must GOVERN
# the table name rather than merely share a string with it. ``from``/``update``
# are ordinary English words: clause adjacency alone made prose such as
# "outcome is retired from token_outcomes" look like SQL. The two-stage shape
# keeps hand-written SELECT/JOIN/INSERT/UPDATE/DELETE/DDL statements while
# rejecting docstrings and message text that happen to use a preposition next
# to the table name. WITH is deliberately structural rather than a bare leading
# word because natural English commonly starts with "With".
_SQL_IDENTIFIER = r"""(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]+"|`[^`]+`|\[[^\]]+\])"""
_TOKEN_OUTCOMES_TABLE = rf"(?:{_SQL_IDENTIFIER}\s*\.\s*)?[\"`\[]?token_outcomes\b[\"`\]]?"
_SELECT_TABLE_SUFFIX = (
    rf"(?:\s+(?:as\s+)?{_SQL_IDENTIFIER})?\s*"
    r"(?=\Z|[;,)]|\b(?:where|join|on|order|group|having|limit|offset|union|except|intersect|returning)\b)"
)
TOKEN_OUTCOMES_SQL_DIRECT_RES = (
    re.compile(rf"\Aselect\b[\s\S]*?\b(?:from|join)\s+{_TOKEN_OUTCOMES_TABLE}{_SELECT_TABLE_SUFFIX}", re.IGNORECASE),
    re.compile(
        rf"\Ainsert(?:\s+or\s+(?:replace|abort|fail|ignore|rollback))?\s+into\s+{_TOKEN_OUTCOMES_TABLE}\s*"
        r"(?=\(|\b(?:default|values|select|overriding)\b)",
        re.IGNORECASE,
    ),
    re.compile(rf"\Aupdate\s+{_TOKEN_OUTCOMES_TABLE}\s+set\s+{_SQL_IDENTIFIER}\s*=", re.IGNORECASE),
    re.compile(rf"\Adelete\s+from\s+{_TOKEN_OUTCOMES_TABLE}\s*(?=\Z|;|\b(?:where|returning)\b)", re.IGNORECASE),
    re.compile(rf"\Acreate\s+table(?:\s+if\s+not\s+exists)?\s+{_TOKEN_OUTCOMES_TABLE}\s*(?=\(|\b(?:as|like)\b)", re.IGNORECASE),
    re.compile(rf"\Aalter\s+table\s+{_TOKEN_OUTCOMES_TABLE}\s+(?:add|drop|rename|alter|set)\b", re.IGNORECASE),
    re.compile(rf"\Areplace\s+into\s+{_TOKEN_OUTCOMES_TABLE}\s*(?=\(|\b(?:values|select|set)\b)", re.IGNORECASE),
)
SQL_LEADING_TRIVIA_RE = re.compile(r"\A(?:\s|--[^\r\n]*(?:\r?\n|\Z)|/\*[\s\S]*?\*/)*")
SQL_EXPLAIN_PREFIX_RE = re.compile(r"\Aexplain(?:\s+query\s+plan)?\s+", re.IGNORECASE)
SQL_CTE_START_RE = re.compile(
    rf"\Awith(?:\s+recursive)?\s+{_SQL_IDENTIFIER}\s*(?:\([^)]*\))?\s+as\s*\(",
    re.IGNORECASE,
)
SQL_CTE_ENTRY_RE = re.compile(
    rf"\A{_SQL_IDENTIFIER}\s*(?:\([^)]*\))?\s+as\s*\(",
    re.IGNORECASE,
)
TOKEN_OUTCOME_COLUMN_RE = re.compile(r"\boutcome\b", re.IGNORECASE)
TOKEN_OUTCOME_PATH_RE = re.compile(r"\bpath\b", re.IGNORECASE)
TOKEN_OUTCOME_IS_TERMINAL_RE = re.compile(r"\bis_terminal\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class InventoryFinding:
    """One ADR-019 tests-tree inventory finding."""

    kind: FindingKind
    path: str
    line: int
    col: int
    symbol: str
    context: str

    def to_json_payload(self) -> dict[str, object]:
        """Return the legacy JSON-lines payload shape."""
        payload = asdict(self)
        payload["kind"] = self.kind.value
        return payload


@dataclass(frozen=True, slots=True)
class TestToSourceMappingRule:
    """Run the ADR-019 tests-tree inventory through elspeth-lints."""

    id: str = RULE_ID
    scope: RuleScope = RuleScope.WHOLE_REPO
    metadata: RuleMetadata = RULE_METADATA

    def analyze(self, tree: ast.AST, file_path: Path, context: RuleContext) -> list[LintFinding]:
        """Run the tests-tree inventory scan."""
        del tree, file_path
        return scan_root(
            context.root,
            allowlist_dir_override=context.allowlist_dir_override,
            emit_allowlist_governance=context.emit_allowlist_governance,
        )


class ADR019TestInventoryVisitor(ast.NodeVisitor):
    """AST visitor for stale tests-tree ADR-019 expectations."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.findings: list[InventoryFinding] = []

    def _add(self, kind: FindingKind, node: ast.expr | ast.stmt, symbol: str) -> None:
        line, col = _node_location(node)
        self.findings.append(
            InventoryFinding(
                kind=kind,
                path=self.path,
                line=line,
                col=col,
                symbol=symbol,
                context=_context(node),
            )
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        member = _row_outcome_member(node)
        if member is not None:
            self._add(FindingKind.ROW_OUTCOME_ATTRIBUTE, node, member)

        column = _token_outcomes_column(node)
        if column is not None:
            self._add(FindingKind.TOKEN_OUTCOMES_SCHEMA_READ, node, column)

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "RowOutcome":
            self._add(FindingKind.ROW_OUTCOME_ATTRIBUTE, node, "RowOutcome")
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        self._visit_collection(node)
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._visit_collection(node)
        self.generic_visit(node)

    def visit_Set(self, node: ast.Set) -> None:
        self._visit_collection(node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _is_raw_token_outcomes_sql(node.value):
            self._add(FindingKind.RAW_TOKEN_OUTCOMES_SQL, node, "token_outcomes")
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if all(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            self._visit_equality_compare(node)
        if all(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            self._visit_membership_compare(node)
        self.generic_visit(node)

    def _visit_collection(self, node: ast.List | ast.Tuple | ast.Set) -> None:
        if _contains_row_outcome_member(node):
            self._add(FindingKind.ROW_OUTCOME_COLLECTION, node, "RowOutcome")

    def _visit_equality_compare(self, node: ast.Compare) -> None:
        sides = [node.left, *node.comparators]
        for left, right in pairwise(sides):
            if _contains_row_outcome_member(left) or _contains_row_outcome_member(right):
                self._add(FindingKind.ROW_OUTCOME_COMPARE, node, "RowOutcome")
                continue
            self._maybe_add_old_string_compare(node, left, right)
            self._maybe_add_old_string_compare(node, right, left)

    def _maybe_add_old_string_compare(self, node: ast.Compare, symbol_side: ast.AST, value_side: ast.AST) -> None:
        value = _string_constant(value_side)
        if value is None:
            return
        if _is_outcome_symbol(symbol_side) and value in ROW_OUTCOME_VALUES:
            self._add(FindingKind.OLD_OUTCOME_STRING_COMPARE, node, value)

    def _visit_membership_compare(self, node: ast.Compare) -> None:
        sides = [node.left, *node.comparators]
        for left, right in pairwise(sides):
            if _contains_row_outcome_member(left) or _contains_row_outcome_member(right):
                self._add(FindingKind.ROW_OUTCOME_MEMBERSHIP, node, "RowOutcome")
                continue
            values = _literal_string_values(right)
            if _is_outcome_symbol(left) and values & ROW_OUTCOME_VALUES:
                self._add(FindingKind.OLD_OUTCOME_STRING_MEMBERSHIP, node, ",".join(sorted(values & ROW_OUTCOME_VALUES)))


def scan_root(
    root: Path,
    *,
    allowlist_dir_override: Path | None = None,
    emit_allowlist_governance: bool = True,
) -> list[LintFinding]:
    """Scan tests and apply the ADR-019 test inventory allowlist."""
    tests_root, project_root = tests_scan_roots(root)
    findings = scan_tree(tests_root, project_root)
    allowlist_dir = (
        allowlist_dir_override if allowlist_dir_override is not None else allowlist_path_for_root(root, "test_to_source_mapping")
    )
    loaded = load_allowlist(allowlist_dir, valid_rule_ids={RULE_ID}) if allowlist_dir.exists() else None
    active = _filter_loaded_findings(findings, loaded)
    lint_findings = [to_lint_finding(finding) for finding in active]
    if loaded is None:
        return lint_findings
    return [
        *lint_findings,
        *allowlist_governance_findings_for_root(
            loaded,
            allowlist_dir,
            root=root,
            allowlist_dir_override=allowlist_dir_override,
            enabled=emit_allowlist_governance,
        ),
    ]


def tests_scan_roots(root: Path) -> tuple[Path, Path]:
    """Return the tests scan root and display root."""
    root = root.resolve()
    nested_tests = root / "tests"
    if nested_tests.is_dir():
        return nested_tests, root
    if root.name == "tests":
        return root, root.parent
    return root, root


def scan_tree(root: Path, project_root: Path | None = None) -> list[InventoryFinding]:
    """Return ADR-019 tests-tree inventory findings under root."""
    project_root = project_root or root
    findings: list[InventoryFinding] = []
    for path in iter_python_files(root):
        findings.extend(scan_file(path, project_root))
    return findings


def scan_file(path: Path, project_root: Path | None = None) -> list[InventoryFinding]:
    """Return ADR-019 tests-tree inventory findings for one file."""
    project_root = project_root or path.parent
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    visitor = ADR019TestInventoryVisitor(_display_path(path, project_root))
    visitor.visit(tree)
    return visitor.findings


def filter_findings(findings: Iterable[InventoryFinding], allowlist: Path | None) -> list[InventoryFinding]:
    """Filter tests-tree inventory findings via the unified core allowlist loader."""
    if allowlist is None or not allowlist.exists():
        return list(findings)
    loaded = load_allowlist(allowlist, valid_rule_ids={RULE_ID})
    return _filter_loaded_findings(findings, loaded)


def _filter_loaded_findings(findings: Iterable[InventoryFinding], loaded: Allowlist | None) -> list[InventoryFinding]:
    if loaded is None:
        return list(findings)
    return [
        finding
        for finding in findings
        if loaded.match(
            FindingKey(
                file_path=finding.path,
                rule_id=RULE_ID,
                symbol_context=(),
                fingerprint="",
            )
        )
        is None
    ]


def to_lint_finding(finding: InventoryFinding) -> LintFinding:
    """Convert an inventory finding to the elspeth-lints protocol."""
    return LintFinding(
        rule_id=finding.kind.value,
        file_path=finding.path,
        line=finding.line,
        column=finding.col,
        message=finding_message(finding),
        fingerprint=finding_fingerprint(finding),
        severity=Severity.ERROR,
        suggestion=str(finding_payload(finding)["suggestion"]),
    )


def finding_fingerprint(finding: InventoryFinding) -> str:
    """Return the stable parity fingerprint for one inventory finding."""
    return f"{finding.path}:{finding.kind.value}:{finding.symbol}:{finding.line}:{finding.col}"


def finding_message(finding: InventoryFinding) -> str:
    """Return the parity message for one inventory finding."""
    return f"{finding.kind.value}: {finding.symbol} in {finding.context}"


def finding_payload(finding: InventoryFinding) -> dict[str, object]:
    """Return an elspeth-lints parity-schema payload."""
    return {
        "rule_id": finding.kind.value,
        "file_path": finding.path,
        "line": finding.line,
        "column": finding.col,
        "message": finding_message(finding),
        "fingerprint": finding_fingerprint(finding),
        "severity": "error",
        "suggestion": "Migrate tests to the ADR-019 two-axis outcome/path contract and avoid raw token_outcomes outcome-only SQL.",
    }


def allowlist_path_for_root(root: Path, directory_name: str) -> Path:
    """Find a CI allowlist directory from a scan root or repository cwd."""
    relative = Path("config") / "cicd" / directory_name
    candidates = [root, Path.cwd(), *root.parents]
    for candidate in candidates:
        path = candidate / relative
        if path.exists():
            return path
    return root / relative


def _context(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return node.__class__.__name__


def _node_location(node: ast.expr | ast.stmt) -> tuple[int, int]:
    return node.lineno, node.col_offset


def _display_path(path: Path, project_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _string_constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_string_values(node: ast.AST) -> frozenset[str]:
    if not isinstance(node, ast.List | ast.Tuple | ast.Set):
        return frozenset()
    values = {_string_constant(element) for element in node.elts}
    return frozenset(value for value in values if value is not None)


def _is_outcome_symbol(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "outcome"
    if isinstance(node, ast.Attribute):
        return node.attr == "outcome"
    return False


def _row_outcome_member(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Attribute):
        return None
    if not isinstance(node.value, ast.Name):
        return None
    if node.value.id != "RowOutcome":
        return None
    return node.attr


def _contains_row_outcome_member(node: ast.AST) -> bool:
    return any(_row_outcome_member(child) is not None for child in ast.walk(node))


def _token_outcomes_column(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Attribute):
        return None
    if node.attr != "is_terminal":
        return None
    value = node.value
    if not isinstance(value, ast.Attribute) or value.attr != "c":
        return None
    table = value.value
    if not isinstance(table, ast.Name) or table.id != "token_outcomes_table":
        return None
    return node.attr


def _matching_sql_parenthesis(value: str, open_index: int) -> int | None:
    """Return the balanced SQL close, ignoring quoted and commented parentheses."""
    depth = 1
    index = open_index + 1
    while index < len(value):
        char = value[index]
        following = value[index + 1] if index + 1 < len(value) else ""
        if char == "-" and following == "-":
            newline = value.find("\n", index + 2)
            index = len(value) if newline < 0 else newline + 1
            continue
        if char == "/" and following == "*":
            comment_end = value.find("*/", index + 2)
            if comment_end < 0:
                return None
            index = comment_end + 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            while index < len(value):
                if value[index] == "\\":
                    index += 2
                    continue
                if value[index] == quote:
                    if index + 1 < len(value) and value[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                return None
            continue
        if char == "[":
            index += 1
            while index < len(value):
                if value[index] == "]":
                    if index + 1 < len(value) and value[index + 1] == "]":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                return None
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _sql_code_without_literals_or_comments(value: str) -> str:
    """Mask SQL string literals and comments while preserving code offsets."""
    masked = list(value)
    index = 0
    while index < len(value):
        following = value[index + 1] if index + 1 < len(value) else ""
        if value[index] == "-" and following == "-":
            end = value.find("\n", index + 2)
            end = len(value) if end < 0 else end
        elif value[index] == "/" and following == "*":
            comment_end = value.find("*/", index + 2)
            end = len(value) if comment_end < 0 else comment_end + 2
        elif value[index] == "'":
            end = index + 1
            while end < len(value):
                if value[end] == "\\":
                    end += 2
                    continue
                if value[end] == "'":
                    if end + 1 < len(value) and value[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
        else:
            index += 1
            continue
        masked[index:end] = " " * (end - index)
        index = end
    return "".join(masked)


def _sql_statement_candidates(value: str) -> tuple[str, ...]:
    """Return direct statements from a plain statement or a balanced CTE chain."""
    cte_entry = SQL_CTE_START_RE.match(value)
    if cte_entry is None:
        return (value,)

    candidates: list[str] = []
    remaining = value
    while cte_entry is not None:
        cte_close = _matching_sql_parenthesis(remaining, cte_entry.end() - 1)
        if cte_close is None:
            return ()
        candidates.append(remaining[cte_entry.end() : cte_close])
        tail = remaining[cte_close + 1 :].lstrip()
        if not tail.startswith(","):
            candidates.append(tail)
            return tuple(candidates)
        remaining = tail[1:].lstrip()
        cte_entry = SQL_CTE_ENTRY_RE.match(remaining)

    return ()


def _is_raw_token_outcomes_sql(value: str) -> bool:
    """Return whether a string constant hand-writes SQL against ``token_outcomes``.

    The string must contain a SQL statement whose clause keyword governs the
    table itself; the ADR-019 two-axis check (outcome without path) then
    decides whether that statement is the stale outcome-only shape.
    """
    sql_code = _sql_code_without_literals_or_comments(value)
    candidate = SQL_LEADING_TRIVIA_RE.sub("", sql_code, count=1)
    explain_prefix = SQL_EXPLAIN_PREFIX_RE.match(candidate)
    if explain_prefix is not None:
        candidate = candidate[explain_prefix.end() :]
    statement_candidates = _sql_statement_candidates(candidate)
    if not any(pattern.search(statement) is not None for statement in statement_candidates for pattern in TOKEN_OUTCOMES_SQL_DIRECT_RES):
        return False
    if TOKEN_OUTCOME_IS_TERMINAL_RE.search(sql_code) is not None:
        return True
    return TOKEN_OUTCOME_COLUMN_RE.search(sql_code) is not None and TOKEN_OUTCOME_PATH_RE.search(sql_code) is None


RULE = TestToSourceMappingRule()
