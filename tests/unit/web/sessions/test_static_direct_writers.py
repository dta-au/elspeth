"""Mechanical direct-writer guard for ``chat_messages`` and ``composition_states``.

Phase 1A introduces NOT NULL ``chat_messages.sequence_no``,
``chat_messages.writer_principal``, and ``composition_states.provenance``
columns. Every direct insert into either table must either route through
the new lock-aware helpers (``_insert_chat_message``,
``_insert_composition_state``, ``_reserve_sequence_range``) or be
explicitly allowlisted as a known semantic site (schema test,
corruption fixture, OperationalError canary, standalone eval fixture).

This guard is the **mechanical merge gate** for the Schedule 1A cutover.
A reviewer-facing ripgrep is fast but cannot distinguish a corruption
fixture from a real bypass; this scanner walks the AST, identifies the
enclosing function/class qualified symbol, and matches against a static
allowlist keyed by ``(path, enclosing_symbol, table, operation)`` **and
the number of writes reviewed at that key**. The count is part of the
key: a set of 4-tuples cannot say "exactly one reviewed write here", so
without it a second write added inside an already-reviewed function is
indistinguishable from the first (elspeth-7eac6c2e24).

Coverage:

* SQLAlchemy ``insert(table_name)`` calls, including a qualified table
  argument (``insert(models.table_name)``), a qualified ``insert``
  callable (``sa.insert(table_name)``, ``sa.sql.insert(table_name)``),
  and an ``insert`` imported under another name (``from
  sqlalchemy.dialects.sqlite import insert as sqlite_insert;
  sqlite_insert(table_name)``) — the callable is resolved from the file's
  imports, not matched by spelling; see :func:`_imported_insert_names`.
* SQLAlchemy ``table_name.insert()`` chained calls, including the
  qualified ``models.table_name.insert()`` spelling.
* Raw ``INSERT INTO chat_messages`` / ``INSERT INTO composition_states``
  string literals, regardless of enclosing call (covers
  ``cursor.execute``, ``cursor.executemany``, ``exec_driver_sql``,
  ``OperationalError(...)`` canaries, and bare strings), including the
  SQLite upsert spellings ``INSERT OR IGNORE INTO`` / ``INSERT OR REPLACE
  INTO`` / ``REPLACE INTO``, a schema prefix (``main.chat_messages``) and
  a quoted identifier (``"chat_messages"``). A table name assembled at
  runtime (``"INSERT INTO " + name``) is not a literal and is not seen.

Both spellings of a table reference classify to the same ``table`` and
``operation``, so the allowlist key does not depend on how a writer
happens to import the table (elspeth-9b3cf0d52d). Aliasing a table to a
local name is out of scope; see :func:`_tracked_table_identifier`.

The reviewed inventory is a literal committed in this file. It is never
derived from the tree it checks — see the comment above
``_TEST_FIXTURE_REVIEWED_WRITERS`` for what happened when it was.

Lock-discipline conditional-dormancy rule:

The plan introduces ``_session_write_lock``, ``_reserve_sequence_range``,
``_insert_chat_message``, ``_insert_composition_state``, and
``_assert_session_write_lock_held`` in Tasks 7-10. Until those symbols
exist in the codebase, the lock-discipline and inline-allocation checks
are dormant (vacuous PASS). They activate the moment ``_session_write_lock``
is defined anywhere under ``src/``, fail-closed against any caller that
drifts off the lock.

Self-exclusion:

This file contains the table identifier strings as scanner data; without
self-exclusion the live-tree scan would find its own data. The scanner
skips any source file whose resolved path equals this module's resolved
path.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from tests.helpers.tree_gate import ParsedPythonFile, iter_gate_sources

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TABLE_IDENTIFIER_TO_NAME = {
    "chat_messages_table": "chat_messages",
    "composition_states_table": "composition_states",
}

_RAW_SQL_INSERT_PATTERN = re.compile(
    r"(?:INSERT\s+(?:OR\s+\w+\s+)?|REPLACE\s+)INTO\s+(?:\w+\.)?[\"`]?(chat_messages|composition_states)\b",
    re.IGNORECASE,
)

_LOCK_HELPER_NAMES = (
    "_reserve_sequence_range",
    "_insert_chat_message",
    "_insert_composition_state",
)

_SESSION_WRITE_LOCK_NAME = "_session_write_lock"
_LOCK_HELD_ASSERT_NAME = "_assert_session_write_lock_held"
_PAIRED_SESSION_WRITE_LOCK_NAME = "_session_pair_locked_begin"

# Resolved at module import to avoid recomputing per-test.
_SCANNER_SELF_PATH = Path(__file__).resolve()


def _find_repo_root() -> Path:
    """Return the repository root resolved from this test file.

    ``tests/unit/web/sessions/test_static_direct_writers.py``'s parents
    chain is ``[sessions/, web/, unit/, tests/, <repo>]`` so
    ``parents[4]`` is the repo root. The ``src``/``tests`` anchors must
    exist directly under it.
    """

    candidate = Path(__file__).resolve().parents[4]
    if not (candidate / "src").is_dir() or not (candidate / "tests").is_dir():
        raise RuntimeError(f"could not resolve repo root from {Path(__file__)}: candidate {candidate} is missing src/ or tests/")
    return candidate


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewedWriter:
    """Allowlist entry for a known-OK direct writer site.

    The four keying fields ``(path, enclosing_symbol, table, operation)``
    must match the scanner's :class:`WriterMatch` exactly, and ``count``
    declares HOW MANY writes of that shape the review blessed. ``purpose``
    is informational and shows up in the violation report when a new site
    fails the gate.

    ``count`` is the multiplicity component of the key
    (elspeth-7eac6c2e24). A *set* of 4-tuples cannot express "exactly one
    reviewed write here", which is what reviewing a writer site actually
    claims. Measured on this tree: adding a second
    ``insert(chat_messages_table)`` inside the already-allowlisted
    ``SessionServiceImpl.fork_session._sync`` reproduced the allowlisted
    4-tuple exactly and was not reported, while the same write spelled
    ``chat_messages_table.insert()`` WAS reported — because that spelling
    classifies as a different ``operation``. The gate therefore caught the
    stylistically inconsistent addition and missed the consistent one,
    inverted from the risk it exists to manage. With ``count``, the
    surplus write fails the gate whichever spelling it uses.

    Multiplicity is a count rather than a per-line entry on purpose. A
    line number churns on every unrelated edit above it, so a line-keyed
    allowlist would go red on commits that change no writer at all, and
    the maintainer response to that noise is a bulk re-baseline — exactly
    the laundering this gate must not teach (the deleted
    ``_expand_dynamic_allowlist`` failed that way). A count changes only
    when the number of writes changes. Each reviewed site's line is
    recorded in ``purpose`` for navigation, where going stale is harmless
    because nothing keys on it.
    """

    path: str
    enclosing_symbol: str
    table: str
    operation: str
    purpose: str
    count: int = 1


@dataclass(frozen=True)
class WriterMatch:
    path: str
    line: int
    enclosing_symbol: str
    table: str
    operation: str
    snippet: str


@dataclass(frozen=True)
class StaleReviewedWriter:
    """A reviewed entry that promises more writer sites than the tree holds.

    Emitted when ``entry.count`` exceeds the number of live matches for
    ``entry``'s key. That is the removal half of drift: a writer was
    deleted or its enclosing function renamed, and the allowlist kept
    vouching for it. Reporting it is what makes the "a removed line shows
    up as a violation" claim true rather than aspirational.
    """

    entry: ReviewedWriter
    found: int


@dataclass(frozen=True)
class LockDisciplineViolation:
    path: str
    line: int
    enclosing_symbol: str
    helper_name: str
    snippet: str


@dataclass(frozen=True)
class InlineAllocViolation:
    path: str
    line: int
    enclosing_symbol: str
    snippet: str


@dataclass(frozen=True)
class LockDisciplineNegativeTest:
    """Allowlist entry for an intentional lock-required-helper call outside ``_session_write_lock``.

    Plan §94 explicitly requires the static guard to support ``negative
    test`` exemptions: each helper has a precondition assertion
    (``_assert_session_write_lock_held``), and verifying that assertion
    fires REQUIRES calling the helper without the lock. The three keying
    fields ``(path, enclosing_symbol, helper_name)`` must match the
    scanner's :class:`LockDisciplineViolation` exactly. ``purpose`` is
    informational and shows up in the violation report when a new site
    fails the gate.
    """

    path: str
    enclosing_symbol: str
    helper_name: str
    purpose: str


# ---------------------------------------------------------------------------
# AST utilities
# ---------------------------------------------------------------------------


ParentMap = dict[ast.AST, ast.AST]
"""Child-node to parent-node lookup for one parsed module.

``ast`` doesn't track parents and the checkers below need upward walks (from a
Call to its enclosing FunctionDef, ``with`` block, or statement list). The map
is built once per file by :func:`_parent_map` and threaded explicitly. It
replaces the older "attach a ``.parent`` attribute to every node" trick: an
attached attribute is invisible to the type checker, forces a sentinel
``getattr`` at every read, and silently returns ``None`` for a node from a
*different* tree instead of raising.
"""


def _parent_map(tree: ast.AST) -> ParentMap:
    """Build the child to parent lookup for ``tree`` in one walk.

    The root itself is absent from the map, so ``parents.get(node)`` returning
    ``None`` means exactly "``node`` is the module root", not "``node`` might
    be from somewhere else".
    """

    parents: ParentMap = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _qualified_symbol(node: ast.AST, parents: ParentMap) -> str:
    """Return the dotted enclosing-symbol path for ``node``.

    Walks ``parents``. The result joins ``ClassDef`` and
    ``FunctionDef``/``AsyncFunctionDef`` names from outermost to
    innermost. Module-level nodes return ``"<module>"``.
    """

    parts: list[str] = []
    cursor: ast.AST | None = node
    while cursor is not None:
        if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(cursor.name)
        cursor = parents.get(cursor)
    if not parts:
        return "<module>"
    return ".".join(reversed(parts))


def _enclosing_call(node: ast.AST, parents: ParentMap) -> ast.Call | None:
    """Walk parents from ``node`` up to the nearest enclosing :class:`ast.Call`.

    Returns ``None`` if the node is not inside a call. Skips
    Module/FunctionDef/ClassDef boundaries by continuing past them.
    """

    cursor: ast.AST | None = parents.get(node)
    while cursor is not None:
        if isinstance(cursor, ast.Call):
            return cursor
        cursor = parents.get(cursor)
    return None


def _enclosing_with_blocks(node: ast.AST, parents: ParentMap) -> Iterator[ast.With | ast.AsyncWith]:
    """Yield enclosing ``with`` / ``async with`` blocks from inner to outer.

    Used by the lock-discipline checker to verify a helper call is
    inside a ``_session_write_lock`` context manager. Stops at the
    enclosing FunctionDef boundary so unrelated outer ``with`` blocks
    in the same module don't satisfy the check.
    """

    cursor: ast.AST | None = parents.get(node)
    while cursor is not None and not isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
        if isinstance(cursor, (ast.With, ast.AsyncWith)):
            yield cursor
        cursor = parents.get(cursor)


def _call_callable_name(call: ast.Call) -> str:
    """Return a human-readable name for the callable of an :class:`ast.Call`.

    For ``foo.bar(x)`` returns ``"bar"``. For ``foo(x)`` returns ``"foo"``.
    For complex expressions returns ``"<expr>"``.
    """

    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return "<expr>"


def _tracked_table_identifier(node: ast.expr) -> str | None:
    """Return the tracked table identifier ``node`` names, or ``None``.

    Both spellings a writer can use to reach a tracked table are
    recognised (elspeth-9b3cf0d52d):

    * ``chat_messages_table`` — ``from ...models import chat_messages_table``
    * ``models.chat_messages_table`` — ``from ... import models``, qualified

    They name the same table, so both classify identically. Giving the
    qualified spelling its own table or operation string would rebuild the
    asymmetry this guard exists to close: a writer could then evade a
    reviewed key just by changing how it spells the import.

    TABLE aliasing (``cmt = chat_messages_table; insert(cmt)``) stays out
    of scope: resolving it needs dataflow analysis, and unlike a qualified
    import — which is the ordinary house style in
    ``tests/unit/web/sessions/`` — a local rebinding of a table object is
    conspicuous in review. CALLABLE aliasing (``from
    sqlalchemy.dialects.sqlite import insert as sqlite_insert``) is NOT
    out of scope: it is live house style for upserts in this subsystem
    (``src/elspeth/web/secrets/user_store.py``) and is resolved from the
    file's imports by :func:`_imported_insert_names`.
    """

    if isinstance(node, ast.Name) and node.id in _TABLE_IDENTIFIER_TO_NAME:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in _TABLE_IDENTIFIER_TO_NAME:
        return node.attr
    return None


def _line_snippet(source_lines: Sequence[str], line: int, max_len: int = 200) -> str:
    """Return a single-line snippet for ``line`` (1-indexed), truncated."""

    if 1 <= line <= len(source_lines):
        snippet = source_lines[line - 1].strip()
        if len(snippet) > max_len:
            return snippet[: max_len - 3] + "..."
        return snippet
    return ""


# ---------------------------------------------------------------------------
# Direct-writer scanner
# ---------------------------------------------------------------------------


def _imported_insert_names(tree: ast.AST) -> frozenset[str]:
    """Return every local name that ``from <module> import insert [as X]`` binds in ``tree``.

    The bare name ``insert`` is always included. A gate that matches the
    SPELLING ``insert`` is laundered by an import alias: ``from
    sqlalchemy.dialects.sqlite import insert as sqlite_insert`` makes the
    callable ``sqlite_insert``, which a spelling test never fires on, so
    the write is outside the gate entirely — not on a different key,
    invisible. Measured on this tree: ``import insert as`` appears 34
    times in ``src/`` and ``tests/``, three of them in
    ``src/elspeth/web/secrets/user_store.py`` for the sqlite / postgresql /
    mysql upsert dialects. Resolving the callable from the imports is what
    closes it. The walk covers the whole module, so an import inside a
    function body (``user_store.py`` style) is seen too.

    Only ``ImportFrom`` needs resolving: ``import sqlalchemy as sa`` yields
    an ``ast.Attribute`` callable whose ``attr`` is still ``insert``, which
    :func:`_call_callable_name` already returns.
    """

    names = {"insert"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "insert":
                    names.add(alias.asname or "insert")
    return frozenset(names)


class _WriterCollector(ast.NodeVisitor):
    """Collect direct-writer matches for one source file."""

    def __init__(self, rel_path: str, source: str, tree: ast.AST, parents: ParentMap) -> None:
        self.rel_path = rel_path
        self.source_lines = source.splitlines()
        self.tree = tree
        self.parents = parents
        self.matches: list[WriterMatch] = []
        self._insert_names = _imported_insert_names(tree)

    def collect(self) -> list[WriterMatch]:
        self.visit(self.tree)
        return self.matches

    # ------------------------------------------------------------------
    # SQLAlchemy patterns
    # ------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        # Pattern 1: insert(chat_messages_table) or the qualified
        # insert(models.chat_messages_table) — an ``insert`` call whose first
        # argument names a tracked table identifier either way. The callable
        # is resolved from the file's IMPORTS (``self._insert_names``) rather
        # than by ``isinstance(func, ast.Name)`` or by the spelling ``insert``,
        # so a qualified callable (``sa.insert(table)``, ``sa.sql.insert(table)``)
        # and an aliased one (``from sqlalchemy.dialects.sqlite import insert
        # as sqlite_insert; sqlite_insert(table)``) are both covered: each is
        # house-legal here, and either let a writer leave the gate entirely by
        # changing how it spells the import. Requiring ``node.args`` keeps the
        # no-argument ``table.insert()`` form out of this pattern; Pattern 2
        # below owns it. Measured after the widenings: the whole-tree scan over
        # ``src/`` and ``tests/`` went 114 -> 115 matches, the one addition
        # being an aliased ``_insert(chat_messages_table)`` in
        # ``tests/integration/pipeline/test_composer_runtime_agreement.py``
        # that no earlier form of this gate could see; nothing in THIS tree is
        # emitted by both patterns. A hypothetical ``table_a.insert(table_b)``
        # would be, and is not defended against.
        func = node.func
        if _call_callable_name(node) in self._insert_names and node.args:
            identifier = _tracked_table_identifier(node.args[0])
            if identifier is not None:
                self._emit(
                    node,
                    table=_TABLE_IDENTIFIER_TO_NAME[identifier],
                    operation="sqlalchemy_insert_call",
                )

        # Pattern 2: chat_messages_table.insert() or the qualified
        # models.chat_messages_table.insert() — ``.insert`` invoked on
        # something that names a tracked table identifier.
        if isinstance(func, ast.Attribute) and func.attr == "insert":
            identifier = _tracked_table_identifier(func.value)
            if identifier is not None:
                self._emit(
                    node,
                    table=_TABLE_IDENTIFIER_TO_NAME[identifier],
                    operation="sqlalchemy_table_insert",
                )

        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Raw SQL patterns
    # ------------------------------------------------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str):
            return
        match = _RAW_SQL_INSERT_PATTERN.search(node.value)
        if match is None:
            return
        table = match.group(1).lower()
        enclosing = _enclosing_call(node, self.parents)
        operation = f"raw_string_in_{_call_callable_name(enclosing)}" if enclosing is not None else "raw_string_module"
        self._emit(node, table=table, operation=operation)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _emit(self, node: ast.expr, *, table: str, operation: str) -> None:
        line = node.lineno
        self.matches.append(
            WriterMatch(
                path=self.rel_path,
                line=line,
                enclosing_symbol=_qualified_symbol(node, self.parents),
                table=table,
                operation=operation,
                snippet=_line_snippet(self.source_lines, line),
            )
        )


def _iter_python_files(roots: Sequence[Path]) -> Iterator[tuple[Path, ParsedPythonFile]]:
    """Yield ``(root, parsed)`` for every Python source under each root.

    Enumeration, reading, and parsing go through the whole-tree gate helper.
    Skips this scanner module via resolved-path equality.
    """

    for root in roots:
        if not root.exists():
            continue
        for parsed in iter_gate_sources(root):
            if parsed.path.resolve() == _SCANNER_SELF_PATH:
                continue
            yield root, parsed


def scan_writers(
    roots: Sequence[Path],
    *,
    path_anchor: Path | None = None,
) -> list[WriterMatch]:
    """Scan every Python source under ``roots`` for direct writer sites.

    Returns matches with ``path`` made relative to ``path_anchor`` if
    given, else relative to the root that contained each file.
    """

    matches: list[WriterMatch] = []
    for root, parsed in _iter_python_files(roots):
        py_file, source, tree = parsed.path, parsed.source, parsed.tree
        parents = _parent_map(tree)

        anchor = path_anchor or root
        try:
            rel = py_file.resolve().relative_to(anchor.resolve()).as_posix()
        except ValueError:
            rel = py_file.resolve().as_posix()

        matches.extend(_WriterCollector(rel, source, tree, parents).collect())
    return matches


WriterKey = tuple[str, str, str, str]
"""``(path, enclosing_symbol, table, operation)`` — the shape of a writer site."""


def _match_key(match: WriterMatch) -> WriterKey:
    return (match.path, match.enclosing_symbol, match.table, match.operation)


def _entry_key(entry: ReviewedWriter) -> WriterKey:
    return (entry.path, entry.enclosing_symbol, entry.table, entry.operation)


def reviewed_counts(allowlist: Sequence[ReviewedWriter]) -> dict[WriterKey, int]:
    """Index the allowlist as ``key -> reviewed write count``, fail-closed.

    Duplicate keys raise instead of collapsing. Building this with a plain
    dict or set comprehension would silently keep only one of them, which
    is how the pre-``count`` allowlist lost a real review: two entries for
    ``TestCompositionStateUniqueConstraint.test_duplicate_version_raises``
    said "line 126" and "second direct row in the same test", and the set
    key merged them back into one. The reviewer's intent was multiplicity
    all along; the structure could not hold it. Express it as ``count=N``
    on a single entry.
    """

    counts: dict[WriterKey, int] = {}
    for entry in allowlist:
        key = _entry_key(entry)
        if key in counts:
            raise AssertionError(
                f"duplicate reviewed-writer entry for {key}. Two entries with the same "
                f"(path, enclosing_symbol, table, operation) cannot both be honoured: "
                f"express multiplicity as count=N on a single entry."
            )
        if entry.count < 1:
            raise AssertionError(f"reviewed-writer entry for {key} declares count={entry.count}; delete the entry instead.")
        counts[key] = entry.count
    return counts


def violations(
    matches: Sequence[WriterMatch],
    allowlist: Sequence[ReviewedWriter],
) -> list[WriterMatch]:
    """Return writer sites beyond the reviewed count for their shape.

    A match is a violation when its ``(path, enclosing_symbol, table,
    operation)`` key is unreviewed, OR when it is the ``count + 1``-th
    match of a key the review blessed only ``count`` times. Matches are
    consumed in source order so the surplus reported is the later line,
    which is the one a reader has to justify.
    """

    allowed = reviewed_counts(allowlist)
    seen: dict[WriterKey, int] = {}
    surplus: list[WriterMatch] = []
    for match in sorted(matches, key=lambda m: (m.path, m.line)):
        key = _match_key(match)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > allowed.get(key, 0):
            surplus.append(match)
    return surplus


def stale_reviewed_writers(
    matches: Sequence[WriterMatch],
    allowlist: Sequence[ReviewedWriter],
) -> list[StaleReviewedWriter]:
    """Return reviewed entries the live tree no longer backs.

    Only meaningful against a whole-tree scan: an entry looks stale under
    any scan that does not cover its path. :func:`violations` is the
    subset-safe half.
    """

    seen: dict[WriterKey, int] = {}
    for match in matches:
        key = _match_key(match)
        seen[key] = seen.get(key, 0) + 1

    stale: list[StaleReviewedWriter] = []
    for entry in allowlist:
        found = seen.get(_entry_key(entry), 0)
        if found < entry.count:
            stale.append(StaleReviewedWriter(entry=entry, found=found))
    return stale


# ---------------------------------------------------------------------------
# Lock-discipline checker
# ---------------------------------------------------------------------------


def _codebase_defines_symbol(symbol: str, roots: Sequence[Path]) -> bool:
    """Return True iff a top-level ``def <symbol>`` exists under any root.

    Used by the conditional-dormancy rule. A live ``_session_write_lock``
    function definition activates the lock-discipline checks; until it
    exists, the checks return no violations.
    """

    for _, parsed in _iter_python_files(roots):
        for node in ast.walk(parsed.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
                return True
    return False


def _with_block_establishes_session_write_lock(with_node: ast.With | ast.AsyncWith, writer: ast.Call) -> bool:
    """Return True iff a context locks this writer's exact connection/session."""

    writer_conn = _argument(writer, 0, "conn")
    writer_session = _argument(writer, 1, "session_id")
    if writer_conn is None or writer_session is None:
        return False

    for item in with_node.items:
        ctx = item.context_expr
        if not isinstance(ctx, ast.Call):
            continue
        func_name = _call_callable_name(ctx)
        if func_name == _SESSION_WRITE_LOCK_NAME:
            if _same_expression(writer_conn, _argument(ctx, 0, "conn")) and _same_expression(
                writer_session,
                _argument(ctx, 1, "session_id"),
            ):
                return True
        elif func_name == _PAIRED_SESSION_WRITE_LOCK_NAME:
            if not isinstance(item.optional_vars, ast.expr) or not _same_bound_name(writer_conn, item.optional_vars):
                continue
            first_session = _argument(ctx, 0, "first_session_id")
            second_session = _argument(ctx, 1, "second_session_id")
            if _same_expression(writer_session, first_session) or _same_expression(writer_session, second_session):
                return True
    return False


def _argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((item.value for item in call.keywords if item.arg == keyword), None)


def _same_expression(left: ast.expr | None, right: ast.expr | None) -> bool:
    return left is not None and right is not None and ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def _same_bound_name(use: ast.expr, binding: ast.expr) -> bool:
    """Compare a loaded connection name with a ``with ... as`` store target."""

    return isinstance(use, ast.Name) and isinstance(binding, ast.Name) and use.id == binding.id


def _is_matching_lock_assertion(statement: ast.stmt, writer: ast.Call) -> bool:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return False
    assertion = statement.value
    func = assertion.func
    if not (
        (isinstance(func, ast.Name) and func.id == _LOCK_HELD_ASSERT_NAME)
        or (isinstance(func, ast.Attribute) and func.attr == _LOCK_HELD_ASSERT_NAME)
    ):
        return False
    return _same_expression(_argument(assertion, 0, "conn"), _argument(writer, 0, "conn")) and _same_expression(
        _argument(assertion, 1, "session_id"),
        _argument(writer, 1, "session_id"),
    )


def _dominating_statement_blocks(node: ast.AST) -> tuple[list[ast.stmt], ...]:
    """Return the statement lists of ``node`` that can hold a dominated child.

    Written out per node type rather than reflected over field names: the set
    of blocks whose earlier statements DOMINATE a later one is a deliberate
    choice, not whatever fields happen to be named ``body``/``orelse``/
    ``finalbody``. ``Try.handlers`` is excluded on purpose — a handler is not
    dominated by the ``try`` body, so a child reached through one keeps
    walking upward instead of matching there.
    """

    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.With, ast.AsyncWith)):
        return (node.body,)
    if isinstance(node, (ast.ExceptHandler, ast.match_case)):
        return (node.body,)
    if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        return (node.body, node.orelse)
    if isinstance(node, (ast.Try, ast.TryStar)):
        return (node.body, node.orelse, node.finalbody)
    return ()


def _call_has_dominating_matching_lock_assertion(call: ast.Call, parents: ParentMap) -> bool:
    """Accept only a prior, guaranteed assertion over this call's conn/session."""

    child: ast.AST = call
    parent = parents.get(child)
    while parent is not None:
        for statements in _dominating_statement_blocks(parent):
            if child not in statements:
                continue
            index = statements.index(child)
            if any(_is_matching_lock_assertion(statement, call) for statement in statements[:index]):
                return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return False
        child = parent
        parent = parents.get(parent)
    return False


def check_lock_discipline(
    roots: Sequence[Path],
    *,
    path_anchor: Path | None = None,
    allowlist: Sequence[LockDisciplineNegativeTest] = (),
) -> list[LockDisciplineViolation]:
    """Check that every call to a lock-required helper is inside ``_session_write_lock``.

    Conditional-dormant: if ``_session_write_lock`` is not defined anywhere
    under ``roots``, returns ``[]``. As soon as the helpers land (Task 9),
    every caller that drifts off the lock is flagged.

    Plan §94 carve-out: callers explicitly listed in ``allowlist`` are
    exempt. The exemption mechanism exists because each helper has a
    precondition assertion that REQUIRES a negative test calling the
    helper outside the lock. The default empty tuple keeps strict
    semantics for callers that do not need exemptions.
    """

    if not _codebase_defines_symbol(_SESSION_WRITE_LOCK_NAME, roots):
        return []

    allowed_keys = {(entry.path, entry.enclosing_symbol, entry.helper_name) for entry in allowlist}

    findings: list[LockDisciplineViolation] = []
    for root, parsed in _iter_python_files(roots):
        py_file, source, tree = parsed.path, parsed.source, parsed.tree
        parents = _parent_map(tree)
        anchor = path_anchor or root
        try:
            rel = py_file.resolve().relative_to(anchor.resolve()).as_posix()
        except ValueError:
            rel = py_file.resolve().as_posix()
        source_lines = source.splitlines()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            helper_name = _called_helper_name(node)
            if helper_name is None:
                continue
            inside_lock = any(_with_block_establishes_session_write_lock(w, node) for w in _enclosing_with_blocks(node, parents))
            if not inside_lock:
                inside_lock = _call_has_dominating_matching_lock_assertion(node, parents)
            if inside_lock:
                continue
            enclosing_symbol = _qualified_symbol(node, parents)
            if (rel, enclosing_symbol, helper_name) in allowed_keys:
                continue
            line = node.lineno
            findings.append(
                LockDisciplineViolation(
                    path=rel,
                    line=line,
                    enclosing_symbol=enclosing_symbol,
                    helper_name=helper_name,
                    snippet=_line_snippet(source_lines, line),
                )
            )
    return findings


def _called_helper_name(call: ast.Call) -> str | None:
    """Return the lock-required helper name if ``call`` invokes one, else ``None``."""

    func = call.func
    if isinstance(func, ast.Name) and func.id in _LOCK_HELPER_NAMES:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in _LOCK_HELPER_NAMES:
        return func.attr
    return None


def check_helper_lock_assertions(
    roots: Sequence[Path],
    *,
    path_anchor: Path | None = None,
) -> list[LockDisciplineViolation]:
    """Check that each lock-required helper's body calls ``_assert_session_write_lock_held``.

    Conditional-dormant: if ``_session_write_lock`` is not defined anywhere
    under ``roots``, returns ``[]``. After Task 9, this enforces that the
    helpers themselves cannot be reached without the lock — even if a
    caller forgot the static check above.
    """

    if not _codebase_defines_symbol(_SESSION_WRITE_LOCK_NAME, roots):
        return []

    findings: list[LockDisciplineViolation] = []
    for root, parsed in _iter_python_files(roots):
        py_file, source, tree = parsed.path, parsed.source, parsed.tree
        parents = _parent_map(tree)
        anchor = path_anchor or root
        try:
            rel = py_file.resolve().relative_to(anchor.resolve()).as_posix()
        except ValueError:
            rel = py_file.resolve().as_posix()
        source_lines = source.splitlines()

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in _LOCK_HELPER_NAMES:
                continue
            asserts = [
                child
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
                and (
                    (isinstance(child.func, ast.Name) and child.func.id == _LOCK_HELD_ASSERT_NAME)
                    or (isinstance(child.func, ast.Attribute) and child.func.attr == _LOCK_HELD_ASSERT_NAME)
                )
            ]
            if asserts:
                continue
            line = node.lineno
            findings.append(
                LockDisciplineViolation(
                    path=rel,
                    line=line,
                    enclosing_symbol=_qualified_symbol(node, parents),
                    helper_name=node.name,
                    snippet=_line_snippet(source_lines, line),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Inline composition_states.version allocation checker
# ---------------------------------------------------------------------------


_INLINE_VERSION_QUALIFIED_SQL = re.compile(
    r"MAX\s*\(\s*composition_states\.version",
    re.IGNORECASE,
)
_INLINE_VERSION_BARE_SQL = re.compile(r"MAX\s*\(\s*version\b", re.IGNORECASE)
_INLINE_VERSION_TABLE_SQL = re.compile(r"\bFROM\s+composition_states\b", re.IGNORECASE)

_INLINE_ALLOCATION_SCOPE = frozenset({"save_composition_state", "set_active_state"})


def _is_docstring_constant(node: ast.Constant, parents: ParentMap) -> bool:
    """Return True iff ``node`` is the docstring literal of its enclosing scope.

    A docstring quoting the allocation SQL (as ``save_composition_state``'s
    own prose does for the version contract) is documentation, not an
    allocation site.
    """

    parent = parents.get(node)
    if not isinstance(parent, ast.Expr):
        return False
    grandparent = parents.get(parent)
    if not isinstance(grandparent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
        return False
    return bool(grandparent.body) and grandparent.body[0] is parent


def _is_raw_state_version_max_sql(value: str) -> bool:
    """Match realistic raw-SQL version allocation, qualified or bare.

    ``MAX(composition_states.version`` matches on its own; the bare
    ``MAX(version`` form (the shape real raw SQL takes, e.g.
    ``SELECT COALESCE(MAX(version), 0) + 1 FROM composition_states``)
    must also name the table in the same string to count.
    """

    if _INLINE_VERSION_QUALIFIED_SQL.search(value):
        return True
    return bool(_INLINE_VERSION_BARE_SQL.search(value)) and bool(_INLINE_VERSION_TABLE_SQL.search(value))


def _is_sqlalchemy_state_version_max_call(node: ast.Call) -> bool:
    """Match ``func.max(composition_states_table.c.version)`` allocation calls.

    The live allocators (``save_composition_state._sync``,
    ``set_active_state._sync``, ``_insert_composition_state``) all use
    this SQLAlchemy form, not raw SQL — a raw-string-only scan is inert
    over every real site (elspeth-13cadbc73d).
    """

    if _call_callable_name(node) != "max":
        return False
    for arg in node.args:
        if (
            isinstance(arg, ast.Attribute)
            and arg.attr == "version"
            and isinstance(arg.value, ast.Attribute)
            and arg.value.attr == "c"
            and isinstance(arg.value.value, ast.Name)
            and arg.value.value.id == "composition_states_table"
        ):
            return True
    return False


def _with_block_invokes_session_write_lock(with_node: ast.With | ast.AsyncWith) -> bool:
    """Name-only lock check for allocation sites.

    ``_with_block_establishes_session_write_lock`` additionally matches
    the writer call's exact conn/session arguments, but an allocation
    site is a ``select``/string expression, not a helper call — there
    are no writer arguments to match against. Presence of a
    ``_session_write_lock`` / ``_session_pair_locked_begin`` context in
    the same function is the invariant this checker can and does
    enforce; conn/session binding for the subsequent INSERT is enforced
    by the writer-level checkers above.
    """

    for item in with_node.items:
        ctx = item.context_expr
        if isinstance(ctx, ast.Call) and _call_callable_name(ctx) in (
            _SESSION_WRITE_LOCK_NAME,
            _PAIRED_SESSION_WRITE_LOCK_NAME,
        ):
            return True
    return False


def check_inline_state_version_allocation(
    roots: Sequence[Path],
    *,
    path_anchor: Path | None = None,
) -> list[InlineAllocViolation]:
    """Reject inline ``composition_states.version`` allocation outside the write lock.

    Closes the review finding that ``save_composition_state`` and
    ``set_active_state`` previously allocated state versions inline
    without holding ``_session_write_lock``. Covers BOTH allocation
    forms — raw ``MAX(...version...)`` SQL strings and the SQLAlchemy
    ``func.max(composition_states_table.c.version)`` call the live
    allocators actually use — and matches the scope functions anywhere
    in the enclosing dotted symbol, because the real sites live in
    nested ``_sync`` closures (``...save_composition_state._sync``).

    Conditional-dormant: returns ``[]`` until ``_session_write_lock``
    is defined.
    """

    if not _codebase_defines_symbol(_SESSION_WRITE_LOCK_NAME, roots):
        return []

    findings: list[InlineAllocViolation] = []
    for root, parsed in _iter_python_files(roots):
        py_file, source, tree = parsed.path, parsed.source, parsed.tree
        parents = _parent_map(tree)
        anchor = path_anchor or root
        try:
            rel = py_file.resolve().relative_to(anchor.resolve()).as_posix()
        except ValueError:
            rel = py_file.resolve().as_posix()
        source_lines = source.splitlines()

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                if not isinstance(node.value, str) or _is_docstring_constant(node, parents):
                    continue
                if not _is_raw_state_version_max_sql(node.value):
                    continue
            elif isinstance(node, ast.Call):
                if not _is_sqlalchemy_state_version_max_call(node):
                    continue
            else:
                continue
            symbol = _qualified_symbol(node, parents)
            if not _INLINE_ALLOCATION_SCOPE & set(symbol.split(".")):
                continue
            inside_lock = any(_with_block_invokes_session_write_lock(w) for w in _enclosing_with_blocks(node, parents))
            if inside_lock:
                continue
            line = node.lineno
            findings.append(
                InlineAllocViolation(
                    path=rel,
                    line=line,
                    enclosing_symbol=symbol,
                    snippet=_line_snippet(source_lines, line),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Reviewed allowlist (§57-68 of the Phase 1A plan, validated 2026-05-08)
# ---------------------------------------------------------------------------

_REVIEWED_ALLOWLIST: tuple[ReviewedWriter, ...] = (
    # ------ src/elspeth/web/sessions/service.py ------
    # NOTE: ``SessionServiceImpl.add_message._sync`` no longer contains an
    # inline ``insert(chat_messages_table)``. Task 14's rewrite (plan §3174-
    # 3268) routes the writer through ``_insert_chat_message`` under
    # ``_session_write_lock`` after a ``_reserve_sequence_range`` allocation.
    # The corresponding ``ReviewedWriter`` entry that previously sat here has
    # been removed because keeping a stale entry for a writer that no longer
    # exists violates the "do not leave stale promises" rule (Task 10
    # handover pitfall §5).
    #
    # The same rule retired three more entries at the multi-replica rebase
    # (2026-09-05): ``save_composition_state._sync``, ``set_active_state._sync``
    # and ``fork_session._sync`` no longer write directly — those writers moved
    # to ``src/elspeth/web/coordination/repository.py`` and are listed under
    # their new symbols below. ``stale_reviewed_writers()`` reported all three.
    ReviewedWriter(
        path="src/elspeth/web/sessions/service.py",
        enclosing_symbol="SessionServiceImpl._insert_chat_message",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Task 9 chat-row writer (plan §1850-2110): the canonical chat_messages "
            "writer. Task 14's call-site sweep routed every prior production writer "
            "through this helper (``add_message`` rewrite at plan §3174-3268; the "
            "fork batch copy now lives in ``_ForkChildSessionMutations."
            "append_child_messages`` in coordination/repository.py, listed "
            "below). Caller is required to be inside "
            "_session_write_lock (asserted via _assert_session_write_lock_held) and "
            "to have already obtained sequence_no from _reserve_sequence_range; the "
            "negative precondition test is allowlisted in "
            "_LOCK_DISCIPLINE_NEGATIVE_TESTS."
        ),
    ),
    ReviewedWriter(
        path="src/elspeth/web/coordination/repository.py",
        enclosing_symbol="_RepositoryCompositionStateMutations.append_state",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Lane authority writer (recovered deferred-platform merge): the "
            "COMPOSE-only composition-state facet owns the exact fence/session "
            "check, predecessor custody check, version allocation, and insert "
            "in one serialized mutation transaction (checkpoint bae72a268)."
        ),
    ),
    ReviewedWriter(
        path="src/elspeth/web/coordination/repository.py",
        enclosing_symbol="_RepositoryInterpretationMutations.create_or_reconcile_pending",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Lane interpretation facet: pending-interpretation state writes "
            "run under exact COMPOSE custody inside the repository mutation "
            "transaction; opt-out event+session atomicity is owned here "
            "(checkpoint bc9cb0d5d)."
        ),
    ),
    ReviewedWriter(
        path="src/elspeth/web/coordination/repository.py",
        enclosing_symbol="_ForkChildSessionMutations.insert_child_state",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Fork creation transaction: the staged child's copied state is "
            "inserted under the dual parent/child fork authority inside "
            "_ForkCreationTransaction; settlement later re-proves the exact "
            "operation binding before activation."
        ),
    ),
    ReviewedWriter(
        path="src/elspeth/web/coordination/repository.py",
        enclosing_symbol="_ForkChildSessionMutations.append_child_messages",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Fork creation transaction: the staged child's copied transcript "
            "is appended as one cohort under the dual fork authority in the "
            "same transaction as the child state insert."
        ),
    ),
    ReviewedWriter(
        path="tests/testcontainer/web/test_session_derived_mutations_postgres.py",
        enclosing_symbol="_seed_run_and_blob",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "PostgreSQL derived-mutation fixture: seeds the parent "
            "composition_states row required by run/blob FK constraints before "
            "exercising the production repository mutation authority."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/coordination/test_session_derived_mutations.py",
        enclosing_symbol="_seed_run_and_blob",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Derived-mutation fixture: seeds the parent composition_states row "
            "required by run/blob FK constraints before exercising the "
            "production repository mutation authority on SQLite."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/coordination/test_session_operation_fence.py",
        enclosing_symbol="test_composer_completion_mutations_write_fixed_shapes_under_exact_blob_read",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "fence-suite fixture: seeds a composition_states row so the composer-completion BLOB_READ mutation facet can be exercised against a real latest-state binding."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/coordination/test_session_operation_fence.py",
        enclosing_symbol="test_composer_completion_mutations_enforce_kind_session_and_latest_state",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "fence-suite fixture: seeds the state rows whose kind/session/latest-state refusals the completion facet is being proven against."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/coordination/test_session_operation_fence.py",
        enclosing_symbol="test_composer_completion_released_authority_writes_zero_and_successor_writes_once",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "fence-suite fixture: seeds the state row used to prove a released authority writes zero rows and its successor writes exactly once."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/coordination/test_sqlite_session_operation_authority.py",
        enclosing_symbol="_seed_parent_messages",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "fork-authority fixture: seeds the parent transcript the fork creation transaction copies; the copy itself runs through the production authority."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/coordination/test_sqlite_session_operation_authority.py",
        enclosing_symbol="_mutate_fork",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "fork-authority race harness: issues the competing direct write the fork transaction must fence out; the assertion is that the AUTHORITY refuses it."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/coordination/test_sqlite_session_operation_authority.py",
        enclosing_symbol="test_fork_creation_transaction_refuses_third_session_writes.forbidden",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "fork-authority adversarial probe: attempts a third-session write inside the fork pair transaction to prove the connection registry refuses it."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_schema.py",
        enclosing_symbol="_seed_session_state",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "schema fixture: seeds a composition_states parent row so FK/CHECK constraints can be exercised directly on the owned in-memory engine."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/shareable_reviews/test_service.py",
        enclosing_symbol="test_mark_ready_for_review_rejects_state_superseded_after_readiness_without_side_effects.supersede_after_readiness",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose=(
            "shareable-review race harness: inserts the superseding state row mid-flow to prove readiness marking rejects a superseded state without side effects."
        ),
    ),
    ReviewedWriter(
        path="src/elspeth/web/coordination/run_diagnostics_authority.py",
        enclosing_symbol="RepositoryRunDiagnosticsAuditAuthority.append_audit_messages",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Run-diagnostics appends are exposed only through a handle-free "
            "repository authority. It acquires the canonical same-session "
            "lock before re-proving the live session and exact run/state "
            "binding, then allocates a contiguous sequence block and inserts "
            "the whole cohort inside that single transaction "
            "(elspeth-0fcf68d50f, elspeth-90231248dc). The singular "
            "``append_audit_message`` delegates here as a cohort of one."
        ),
    ),
    ReviewedWriter(
        path="src/elspeth/web/sessions/service.py",
        enclosing_symbol="SessionServiceImpl._insert_composition_state",
        table="composition_states",
        operation="raw_string_module",
        purpose=(
            "Task 10 helper docstring (plan §2112-2751) references the target "
            "table by name in the PRECONDITION/B1/B3 prose; not an actual "
            "write site"
        ),
    ),
    ReviewedWriter(
        path="src/elspeth/web/sessions/service.py",
        enclosing_symbol="SessionServiceImpl._insert_composition_state",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Task 10 composition-state writer (plan §2112-2751): the canonical "
            "composition_states writer for fork-session and (in Phase 3) "
            "compose-loop use. B1 contract — the helper allocates ``version`` "
            "internally via ``SELECT COALESCE(MAX(version),0)+1 WHERE "
            "session_id=:sid`` under the held ``_session_write_lock`` "
            "(asserted via ``_assert_session_write_lock_held``). The "
            "SELECT-MAX-then-INSERT atomicity closes the "
            "fabricated-Tier-1-violation race at the contract boundary "
            "rather than at individual call sites. Sites 403/834 do NOT "
            "route through this helper — see plan §2128-2133 for the "
            "asymmetric-mechanism rationale. Negative precondition test "
            "allowlisted in _LOCK_DISCIPLINE_NEGATIVE_TESTS"
        ),
    ),
    # NOTE: fork_session._sync no longer contains an inline composition_states
    # insert. Task 10 refactored that site to call ``_insert_composition_state``
    # under ``_session_write_lock``. The helper carries its own allowlist entry
    # above ("SessionServiceImpl._insert_composition_state"). The corresponding
    # entry that previously sat here has been removed because keeping a stale
    # ``ReviewedWriter`` for a writer that no longer exists violates the
    # "do not leave stale promises" rule in the Task 10 handover (pitfall §5)
    # and the test-file's "Do not delete reviewed allowlist entries without
    # removing the corresponding writer in the same commit" symmetry.
    # ------ composer custody test fixtures ------
    ReviewedWriter(
        path="tests/integration/web/composer/test_freeform_proposal_prevalidation.py",
        enclosing_symbol="_harness",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Custody integration harness seeds the exact originating user-message row "
            "needed to exercise the blob provenance FK and full proposal loop; no "
            "production sequence allocation is under test in this fixture"
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/blobs/test_service.py",
        enclosing_symbol="_seed_custody_message",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Blob custody unit fixtures require a real same-session message anchor so "
            "the composite provenance FK and deterministic identity are exercised"
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_planner_authoring_aids.py",
        enclosing_symbol="_session_with_user_message",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Authoring-aids exemplar tests seed the originating user chat message the "
            "candidate builder's inline-source custody path anchors to; no production "
            "sequence allocation is under test in this fixture"
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_set_pipeline_candidate.py",
        enclosing_symbol="_session_with_user_message",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Candidate settlement fixture seeds one originating user message to verify "
            "inline-blob provenance without routing setup through the behavior under test"
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_applied_component_echo.py",
        enclosing_symbol="blob_env",
        table="chat_messages",
        operation="sqlalchemy_table_insert",
        purpose=(
            "Applied-component echo fixture seeds the originating user chat row so "
            "create_blob can bind its content as inline custody; the test owns a "
            "test-scoped in-memory engine and sets sequence_no and writer_principal "
            "explicitly, so no production sequence allocation or lock is under test"
        ),
    ),
    # ------ tests/unit/web/sessions/test_models.py — schema test direct rows (7 sites) ------
    # These two writes were previously TWO entries with an identical key.
    # The set-based lookup silently collapsed them to one, so the review's
    # actual claim — "two rows here, deliberately" — could not be stored.
    # ``count=2`` is that same claim in a form the gate can enforce.
    ReviewedWriter(
        path="tests/unit/web/sessions/test_models.py",
        enclosing_symbol="TestCompositionStateUniqueConstraint.test_duplicate_version_raises",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "schema test exercises the composite unique constraint (lines 125, 139): the first "
            "direct row establishes the version, the second re-uses it to drive the violation"
        ),
        count=2,
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_models.py",
        enclosing_symbol="TestSessionForeignKeys.test_chat_message_requires_valid_session",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test exercises chat_messages session_id FK (line 170); direct row required",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_models.py",
        enclosing_symbol="TestSessionForeignKeys.test_orphan_message_rejected_with_fk_enforcement",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test exercises FK enforcement against orphan rows (line 188); direct row required",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_models.py",
        enclosing_symbol="TestCheckConstraints.test_invalid_chat_message_role_rejected",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test exercises chat_messages role CHECK constraint (line 215); direct row required",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_models.py",
        enclosing_symbol="TestCheckConstraints.test_invalid_run_status_rejected",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose="schema test exercises run_status CHECK constraint chain (line 238); composition_state setup row required for run_status assertion",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_models.py",
        enclosing_symbol="TestCheckConstraints.test_invalid_run_event_type_rejected",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose="schema test exercises run_event_type CHECK chain (line 274); composition_state setup row required",
    ),
    # ------ tests/unit/web/sessions/test_service.py — transcript single-connection listener pin (2 sites) ------
    ReviewedWriter(
        path="tests/unit/web/sessions/test_service.py",
        enclosing_symbol="TestAddMessageWithTranscript.test_insert_and_transcript_select_share_one_connection_and_transaction",
        table="chat_messages",
        operation="raw_string_module",
        purpose=(
            "Docstring prose names the INSERT INTO chat_messages statement the "
            "event-listener pin observes; not a write site. The test attaches "
            "before_cursor_execute/commit listeners and asserts the production "
            "add_message_with_transcript insert and transcript SELECT share one "
            "DBAPI connection with no commit between them — it performs no "
            "direct writes of its own."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_service.py",
        enclosing_symbol="TestAddMessageWithTranscript.test_insert_and_transcript_select_share_one_connection_and_transaction",
        table="chat_messages",
        operation="raw_string_in_startswith",
        purpose=(
            "READ-ONLY statement matcher: `statement.lstrip().upper()."
            "startswith('INSERT INTO CHAT_MESSAGES')` classifies statements "
            "observed via the engine event listener to locate the production "
            "writer's own INSERT in the event log. The test issues no direct "
            "insert; the matched statement is emitted by "
            "SessionServiceImpl._insert_chat_message (allowlisted above)."
        ),
    ),
    # ------ tests/unit/web/sessions/test_interpretation_events_table.py — Phase 5b Task 2 schema tests (4 sites) ------
    ReviewedWriter(
        path="tests/unit/web/sessions/test_interpretation_events_table.py",
        enclosing_symbol="_seed_composition_state",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Phase 5b Task 2 schema test helper: seeds a composition_states row "
            "to satisfy the composite FK on interpretation_events. Schema-test "
            "direct insert — no production lock required because the test owns "
            "the in-memory SQLite engine and is exercising DDL/constraint "
            "behaviour, not the production write path. Helper is intentionally "
            "named _seed_composition_state (not _insert_composition_state) so "
            "the lock-discipline scanner does not conflate it with the "
            "production SessionServiceImpl._insert_composition_state."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_interpretation_events_table.py",
        enclosing_symbol="TestCompositionStatesProvenanceEnum.test_invalid_provenance_rejected",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Phase 5b Task 2 schema test: drives the "
            "ck_composition_states_provenance CHECK constraint by inserting an "
            "invalid provenance value directly; bypassing the helper is the "
            "point of the test (the helper would only ever pass valid values)."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_interpretation_events_table.py",
        enclosing_symbol="test_blob_llm_provenance_rejects_blank_strings",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "blob provenance schema test: seeds the created_from_message_id "
            "anchor so the test can isolate the creating_* blank-string CHECK "
            "constraint instead of failing the composite FK first."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/shareable_reviews/test_service.py",
        enclosing_symbol="session_engine_with_row",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose=(
            "Phase 6A Task 5 unit-test fixture: seeds a composition_states row "
            "to satisfy the composite FK on composer_completion_events. The "
            "ShareableReviewService's audit write references "
            "composition_state_id; the test owns the in-memory SQLite engine "
            "and the fixture exists solely to populate the parent row for FK "
            "resolution. No production lock required — the test exercises "
            "service logic, not the production write path."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_composer_completion_events_table.py",
        enclosing_symbol="_seed_composition_state",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Phase 6A schema-test helper: seeds a composition_states row to "
            "satisfy the per-event-type CHECK constraint "
            "ck_composer_completion_events_composition_state_id_required "
            "and the composite FK on composer_completion_events. Helper is "
            "named _seed_composition_state (not _insert_composition_state) "
            "so the lock-discipline scanner does not conflate it with the "
            "production SessionServiceImpl._insert_composition_state. "
            "Schema-test direct insert — no production lock required."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/shareable_reviews/test_telemetry_session_completed.py",
        enclosing_symbol="session_engine_with_row",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose=(
            "Phase 8 Sub-task 7c (telemetry-backfill: phase-6) telemetry test: "
            "seeds a composition_states row so the ShareableReviewService's "
            "mark_ready_for_review audit insert resolves the composite FK on "
            "composer_completion_events. Mirrors the precedent immediately "
            "above (test_service.py session_engine_with_row); the new test "
            "asserts the composer.session.completed_total counter emit at the "
            "service. No production lock required — telemetry-emit test, not a "
            "production write path."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_interpretation_events_table.py",
        enclosing_symbol="TestCompositionStatesProvenanceEnum.test_interpretation_resolve_provenance_accepted",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Phase 5b Task 2 schema test: positive case for the new "
            "'interpretation_resolve' provenance enum value; direct insert "
            "asserts the CHECK constraint accepts the new value."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_interpretation_events_table.py",
        enclosing_symbol="TestTriggerInstalledByBootstrap.test_chat_messages_content_immutable",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Phase 5b Task 2 schema test: seeds a chat_messages row to assert "
            "that trg_chat_messages_immutable_content fires on UPDATE OF "
            "content. Direct insert is required because the test exercises "
            "trigger behaviour, not the production writer."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_interpretation_events_table.py",
        enclosing_symbol="TestTriggerInstalledByBootstrap.test_chat_messages_delete_raises_even_without_blob_reference",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "schema trigger test: seeds a chat_messages row to assert that "
            "trg_chat_messages_no_delete blocks direct DELETE even when no "
            "blob lineage FK exists. Direct insert is required because the "
            "test is isolating trigger behaviour, not exercising the "
            "production writer."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_interpretation_events_table.py",
        enclosing_symbol="TestTriggerInstalledByBootstrap.test_chat_messages_delete_allowed_only_through_session_cascade",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "schema trigger test: seeds a chat_messages row to assert that "
            "whole-session archival may remove transcript rows only through "
            "the sessions-table FK cascade. Direct insert keeps the test "
            "focused on trigger/cascade semantics."
        ),
    ),
    # NOTE: the ``tests/unit/web/sessions/test_fork.py`` corruption-fixture
    # entry (``test_orphaned_chat_message_recovery``, raw_string_in_execute)
    # was removed 2026-09-04: that test no longer exists and the whole file
    # now contains zero writer matches, so the entry vouched for nothing.
    # The stale half of the gate (:func:`stale_reviewed_writers`) reports
    # entries like this instead of tolerating them.
    # ------ tests/unit/evals/lib/test_decode_tools.py — standalone eval fixture ------
    ReviewedWriter(
        path="tests/unit/evals/lib/test_decode_tools.py",
        enclosing_symbol="db_path",
        table="chat_messages",
        operation="raw_string_in_executemany",
        purpose="standalone eval-harness SQLite fixture (line 108) used by evals/lib/decode_tools.py decoder tests; mirrors the real chat_messages schema and seeds rows via raw executemany",
    ),
    ReviewedWriter(
        path="tests/unit/evals/lib/test_decode_tools.py",
        enclosing_symbol="test_result_summary_truncates_above_300_chars",
        table="chat_messages",
        operation="raw_string_in_execute",
        purpose="standalone eval-harness SQLite fixture (line 186) seeds an oversized assistant row to drive the 300-char truncation assertion",
    ),
    ReviewedWriter(
        path="tests/unit/evals/lib/test_decode_tools.py",
        enclosing_symbol="test_decode_tool_sequence_orders_same_timestamp_rows_by_sequence_no",
        table="chat_messages",
        operation="raw_string_in_executemany",
        purpose=(
            "§14.7 / plan §3884 regression: standalone SQLite fixture seeds rows "
            "with same created_at + intentionally non-chronological sequence_no "
            "via raw executemany so the decoder's ORDER BY sequence_no can be "
            "verified independently of the rev-4 service-layer writer path"
        ),
    ),
    # ------ tests/property/web/composer/test_compose_loop_invariants.py — property harness seed ------
    ReviewedWriter(
        path="tests/property/web/composer/test_compose_loop_invariants.py",
        enclosing_symbol="_make_harness",
        table="chat_messages",
        operation="raw_string_in_text",
        purpose=(
            "property harness fixture seeds a prior user row only for the "
            "has_prior_state scenario before driving ComposerServiceImpl's "
            "real compose-loop persistence; raw text keeps the injection arm "
            "mechanically close to the simulated commit/advisory-lock failures"
        ),
    ),
    # ------ tests/unit/web/composer/* — blob-provenance user-message fixtures ------
    ReviewedWriter(
        path="tests/unit/web/composer/test_blob_inline_tools.py",
        enclosing_symbol="blob_env",
        table="chat_messages",
        operation="sqlalchemy_table_insert",
        purpose=(
            "inline-blob tool fixture: seeds the route-level user message "
            "that blob provenance requires before exercising the production "
            "blob tool handlers. The fixture owns the in-memory SQLite engine "
            "and exists only to provide the immutable created_from_message_id "
            "anchor for attribution assertions."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_agent_tooling.py",
        enclosing_symbol="_insert_user_message",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "blob-tool harness fixture: seeds the route-level user message "
            "that create_blob provenance now requires before exercising the "
            "production execute_tool dispatcher. The fixture owns the in-memory "
            "SQLite engine and exists only to satisfy fk_blobs_created_from_"
            "message_session for verbatim content assertions."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_promote_create_blob.py",
        enclosing_symbol="_session_engine_with_user_message",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "create_blob provenance fixture: seeds the triggering user message "
            "so tests can assert verbatim vs LLM-generated blob attribution "
            "against the real blobs_table composite FK."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_promote_set_pipeline.py",
        enclosing_symbol="_session_engine_with_user_message",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "set_pipeline inline-blob provenance fixture: seeds the triggering "
            "user message so inline source blobs can bind created_from_message_id "
            "while the tests focus on blob attribution and state mutation."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_promote_set_source_from_blob.py",
        enclosing_symbol="_session_engine_with_user_message",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "set_source_from_blob provenance fixture: seeds the triggering user "
            "message for blob attribution tests without initialising the full "
            "session route stack."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_promote_update_blob.py",
        enclosing_symbol="_insert_user_message",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "update_blob functional smoke fixture: bootstraps a create_blob "
            "row with the required verbatim user-message anchor before "
            "exercising the production update_blob handler."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_service.py",
        enclosing_symbol="_insert_user_message",
        table="chat_messages",
        operation="sqlalchemy_table_insert",
        purpose=(
            "composer-service fixture helper: creates a deterministic "
            "route-level user message anchor for blob provenance tests while "
            "the service path under test remains the composer loop."
        ),
    ),
    # NOTE: the ``TestComposerTextOnlyResponse.test_blob_only_success_then_
    # empty_state_reply_returns_no_state_mutation_blocker`` entry was removed
    # 2026-09-04: that test no longer exists, and the only remaining writer in
    # test_service.py is the ``_insert_user_message`` helper allowlisted above.
    ReviewedWriter(
        path="tests/unit/web/composer/test_tools.py",
        enclosing_symbol="_insert_user_message",
        table="chat_messages",
        operation="sqlalchemy_table_insert",
        purpose=(
            "composer tool fixture helper: seeds route-level user-message "
            "anchors for verbatim blob provenance assertions while exercising "
            "the production tool handlers."
        ),
    ),
    # ------ tests/unit/web/sessions/* — targeted chat transcript fixtures ------
    ReviewedWriter(
        path="tests/unit/web/sessions/test_blob_inline_resolutions_schema.py",
        enclosing_symbol="_seed_run_and_blob",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "blob-inline-resolution schema fixture: seeds a composition_states "
            "parent row so blob_inline_resolutions FK and CHECK constraints can "
            "be exercised directly. The test owns the in-memory SQLite engine "
            "and is pinning schema behaviour, not the production session writer."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_record_blob_inline_resolutions.py",
        enclosing_symbol="_seed_run_and_blob",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "record_blob_inline_resolutions service fixture: seeds the parent "
            "composition_states row required by the audit table FK before "
            "exercising the production SessionServiceImpl audit writer. Direct "
            "setup keeps the test focused on the audit write and DB-failure "
            "behaviour."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_guided_custody_gate.py",
        enclosing_symbol="TestWriteBoundaryGate.test_guided_revert_refuses_to_copy_a_legacy_unbindable_active_row",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Seeds a composition_states row that predates the pre-persist guided "
            "custody gate (elspeth-4c442aaaa8) so the fenced guided revert's refusal "
            "to re-tip onto it can be pinned; the gate itself blocks the service path. "
            "Renamed with its test when the unfenced set_active_state setter this "
            "originally pinned was removed (fc84028df); same row, same purpose, and "
            "revert_state_for_guided_operation reaches the same custody gate."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_routes.py",
        enclosing_symbol="_insert_legacy_composition_state._sync",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Seeds pre-gate rows carrying deliberately invalid reviewed snapshots so "
            "the YAML export route's read-side rejection stays pinned now that "
            "save_composition_state refuses them (elspeth-4c442aaaa8)."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_service.py",
        enclosing_symbol="TestRunEvents.test_append_and_list_run_events_preserves_order_and_payload",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "0.6.0 run-events service test: seeds the session + composition_"
            "states + runs FK chain so the test can exercise the production "
            "append_run_event / list_run_events path. The composition_states "
            "insert is only the parent-row anchor required by runs_table's "
            "state_id FK; the test owns the in-memory SQLite engine and is "
            "pinning the run-event ordering/payload, not the production "
            "session writer."
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_count_tool_responses_for_assistant.py",
        enclosing_symbol="_persist_assistant_with_tools",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "read-helper fixture seeds one assistant row plus N tool rows to "
            "verify count_tool_responses_for_assistant against persisted "
            "parent_assistant_id/tool_call_id shapes; not a production writer. "
            "Two write SITES (lines 47, 64): the assistant row, then the tool "
            "row inside the per-tool loop"
        ),
        count=2,
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_messages_route_include_tool_rows.py",
        enclosing_symbol="_seed_user_assistant_tool_rows",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "route-view fixture seeds a minimal user/assistant/tool transcript "
            "so include_tool_rows filtering can be verified without invoking "
            "the composer loop"
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_record_audit_grade_view.py",
        enclosing_symbol="_seed_user_assistant_tool_rows",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "audit-grade transcript view fixture seeds a minimal "
            "user/assistant/tool transcript; the test target is the audit "
            "access log/view path, not chat-message writing"
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_messages_route_tool_outcomes.py",
        enclosing_symbol="_seed_compose_turn_with_outcomes",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose=(
            "tool-outcome stamping fixture (elspeth-f5e6723133) seeds one "
            "state row so the applied-call projection can resolve a version "
            "number without invoking the composer loop"
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_messages_route_tool_outcomes.py",
        enclosing_symbol="_seed_compose_turn_with_outcomes",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "tool-outcome stamping fixture (elspeth-f5e6723133) seeds a "
            "user/assistant/tool transcript with primary-writer per-call "
            "state ids; the test target is the GET /messages outcome "
            "projection, not chat-message writing"
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_messages_route_tool_outcomes.py",
        enclosing_symbol="test_envelopes_without_tool_rows_are_left_unstamped",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "tool-outcome stamping negative fixture (elspeth-f5e6723133): an "
            "assistant row whose tool rows never landed must stay unstamped; "
            "seeding that partial shape requires a direct insert"
        ),
    ),
    # ------ tests/unit/web/sessions/test_routes.py — OperationalError canaries ------
    ReviewedWriter(
        path="tests/unit/web/sessions/test_routes.py",
        enclosing_symbol="TestMessageRoutes.test_send_message_llm_call_persistence_failure_raises_on_success_path.flaky_insert",
        table="chat_messages",
        operation="raw_string_in_OperationalError",
        purpose=(
            "Tier-1 audit-corruption regression: the OperationalError's "
            "statement string carries 'INSERT INTO chat_messages' to make "
            "the simulated failure look like a real DB write error; the "
            "test asserts the success-path helper raises AuditIntegrityError "
            "(500) rather than swallowing the failure. Injection moved to "
            "_insert_chat_message when the sidecar cohort became atomic "
            "(elspeth-90231248dc). Not an executed query"
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_routes.py",
        enclosing_symbol="TestMessageRoutes.test_send_message_llm_sidecar_cohort_settles_atomically.flaky_insert",
        table="chat_messages",
        operation="raw_string_in_OperationalError",
        purpose=(
            "Cohort-atomicity canary (elspeth-90231248dc): OperationalError "
            "statement string simulates a mid-cohort chat_messages INSERT "
            "failure on the second LLM-call sidecar; the test asserts zero "
            "sidecars survive (no partial prefix). Not an executed query"
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_persist_compose_turn.py",
        enclosing_symbol="test_add_messages_atomic_mid_cohort_failure_persists_nothing.flaky_insert",
        table="chat_messages",
        operation="raw_string_in_OperationalError",
        purpose=(
            "Cohort-atomicity canary (elspeth-90231248dc) at the service "
            "layer: OperationalError statement string simulates a "
            "chat_messages INSERT failure on the second draft inside "
            "add_messages_atomic; the test asserts the whole cohort rolls "
            "back (zero rows durable, no partial prefix). Not an executed "
            "query"
        ),
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_routes.py",
        enclosing_symbol="TestMessageRoutes.test_send_message_tool_invocation_persistence_failure_raises_on_success_path.flaky_insert",
        table="chat_messages",
        operation="raw_string_in_OperationalError",
        purpose=(
            "Symmetric to the LLM-call Tier-1 canary above. Statement string "
            "is the OperationalError param to simulate a real chat_messages "
            "INSERT failure; the test asserts the success-path tool-invocation "
            "helper raises AuditIntegrityError (500). Injection moved to "
            "_insert_chat_message when the invocation cohort became atomic "
            "(elspeth-90231248dc). Not an executed query"
        ),
    ),
    # NOTE: two ``...flaky_add_message`` canary entries (guided respond and
    # guided chat turn) were removed 2026-09-04: no ``flaky_add_message``
    # exists anywhere in the tree any more. The live OperationalError canaries
    # in test_routes.py all sit in ``...flaky_insert`` symbols, allowlisted
    # above, so the guided-mode coverage is intact under its current name.
    ReviewedWriter(
        path="tests/unit/web/sessions/test_routes.py",
        enclosing_symbol="TestRecomposeConvergencePartialState.test_recompose_convergence_save_operational_error_preserves_422_body._raise_operational",
        table="composition_states",
        operation="raw_string_in_OperationalError",
        purpose="OperationalError canary (line 2448): tests recompose-convergence error translation",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_routes.py",
        enclosing_symbol="TestRecomposeConvergencePartialState.test_recompose_convergence_save_failure_redacts_sqlalchemy_internals._raise_operational",
        table="composition_states",
        operation="raw_string_in_OperationalError",
        purpose="OperationalError canary (line 2524): tests SQL-internal redaction in 422 response",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_routes.py",
        enclosing_symbol="test_runtime_preflight_handler_save_failure_sets_partial_state_save_failed_flag._raise_operational",
        table="composition_states",
        operation="raw_string_in_OperationalError",
        purpose="OperationalError canary (line 5590): tests runtime-preflight save-failure flag",
    ),
    # ------ tests/integration/web/composer/test_inline_source_provenance.py ------
    #
    # Phase 5a Task 2.5 integration test seeds a session + one user
    # chat_messages row directly (no compose loop). These are
    # fixture-only inserts that verify the new
    # ``creation_modality`` / ``created_from_message_id`` /
    # ``creating_*`` columns + composite FK on ``blobs_table``; routing
    # them through ``SessionServiceImpl.add_message`` would require
    # spinning up the full sessions service and offload worker just to
    # land a single deterministic message id, which adds no audit-
    # integrity coverage and obscures the schema-level assertions the
    # test is actually pinning.
    ReviewedWriter(
        path="tests/integration/web/composer/test_inline_source_provenance.py",
        enclosing_symbol="_session_with_user_message",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Phase 5a Task 2.5 inline-source provenance fixture: seeds one "
            "session + one user chat message so the test can assert the new "
            "blobs_table provenance columns (creation_modality, "
            "created_from_message_id, creating_*) and the composite FK "
            "fk_blobs_created_from_message_session. Direct insert keeps the "
            "fixture deterministic (caller controls the message id) and "
            "scope-narrow (no service-stack initialisation)."
        ),
    ),
    ReviewedWriter(
        path="tests/integration/web/composer/test_inline_source_provenance.py",
        enclosing_symbol="test_cross_session_message_id_rejected",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Phase 5a Task 2.5 cross-session FK rejection test: seeds a "
            "second session (B) with its own user message so the test can "
            "drive a blob insert in session A that references session B's "
            "message id. The composite FK fk_blobs_created_from_message_session "
            "must raise IntegrityError; routing through add_message would "
            "obscure the schema-level assertion."
        ),
    ),
    # ------ tests/integration/web/composer/test_chat_messages_attributability.py ------
    #
    # Phase 5a Task 2.6 attributability test seeds a session + one user
    # chat_messages row directly so the blob-provenance / immutable-content
    # assertions have a stable anchor row to bind against. The production
    # write path is still exercised in the same test via
    # ``_prepare_blob_create`` + ``_persist_prepared_blob_create``; the
    # direct insert is only the chat-row anchor, not the system-under-test.
    ReviewedWriter(
        path="tests/integration/web/composer/test_chat_messages_attributability.py",
        enclosing_symbol="_session_with_user_message_and_blob",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "Phase 5a Task 2.6 attributability fixture: seeds one session + "
            "one user chat message so the test can persist a blob via the "
            "real composer write path (_prepare_blob_create + "
            "_persist_prepared_blob_create) and assert the composite FK "
            "fk_blobs_created_from_message_session binds to a stable, "
            "caller-controlled message id. Routing the anchor row through "
            "SessionServiceImpl.add_message would obscure the schema-level "
            "assertions (created_from_message_id immutability, trigger "
            "trg_chat_messages_immutable_content) the test is pinning."
        ),
    ),
    # ------ tests/testcontainer/web/test_schema_probe_postgres.py ------
    # The PostgreSQL trigger proof deliberately seeds the protected rows with
    # raw SQL so it can mutate them through the same low-level connection and
    # prove the database triggers themselves enforce append-only semantics.
    ReviewedWriter(
        path="tests/testcontainer/web/test_schema_probe_postgres.py",
        enclosing_symbol="_seed_postgres_trigger_rows",
        table="composition_states",
        operation="raw_string_in_text",
        purpose=(
            "PostgreSQL trigger fixture: seed the composition-state FK anchor "
            "with raw SQL before directly exercising immutable audit triggers."
        ),
    ),
    ReviewedWriter(
        path="tests/testcontainer/web/test_schema_probe_postgres.py",
        enclosing_symbol="_seed_postgres_trigger_rows",
        table="chat_messages",
        operation="raw_string_in_text",
        purpose=(
            "PostgreSQL trigger fixture: seed the protected chat row with raw "
            "SQL so update/delete trigger enforcement is tested independently "
            "of the session service."
        ),
    ),
)


# Lock-discipline negative-test allowlist (plan §94)
#
# Each lock-required helper (``_reserve_sequence_range``,
# ``_insert_chat_message``, ``_insert_composition_state``) has a
# precondition assertion ``_assert_session_write_lock_held`` that fires
# RuntimeError when invoked outside ``_session_write_lock``. Verifying
# that assertion REQUIRES a test that calls the helper without the
# lock — so by construction the static lock-discipline check would
# flag the test. Plan §94 explicitly authorises an allowlist exemption
# for these specific test sites; this tuple is that exemption surface.
#
# Add a new entry only when adding a corresponding
# ``test_<helper>_requires_session_write_lock`` test that exercises the
# precondition. Removing a helper means removing its negative test AND
# its allowlist entry in the same commit.
_LOCK_DISCIPLINE_NEGATIVE_TESTS: tuple[LockDisciplineNegativeTest, ...] = (
    LockDisciplineNegativeTest(
        path="tests/unit/web/sessions/test_persist_compose_turn.py",
        enclosing_symbol="test_reserve_sequence_range_requires_session_write_lock",
        helper_name="_reserve_sequence_range",
        purpose="negative-precondition test (plan §1623): verifies _assert_session_write_lock_held raises when the lock is not held",
    ),
    LockDisciplineNegativeTest(
        path="tests/unit/web/sessions/test_persist_compose_turn.py",
        enclosing_symbol="test_insert_chat_message_requires_session_write_lock",
        helper_name="_insert_chat_message",
        purpose="negative-precondition test (plan §1924): verifies _assert_session_write_lock_held raises when the lock is not held",
    ),
    LockDisciplineNegativeTest(
        path="tests/unit/web/sessions/test_persist_compose_turn.py",
        enclosing_symbol="test_insert_composition_state_requires_session_write_lock",
        helper_name="_insert_composition_state",
        purpose="negative-precondition test (plan §2471): verifies _assert_session_write_lock_held raises when the lock is not held",
    ),
)


# ---------------------------------------------------------------------------
# Reviewed test-fixture writers (committed literal — DO NOT re-derive)
# ---------------------------------------------------------------------------
#
# READ THIS BEFORE CHANGING ANYTHING BELOW.
#
# These entries were generated once, from a scan of the tree, and then
# COMMITTED as a literal. That is the whole point, and it is one keystroke
# away from the defect it replaces. The predecessor of this tuple was a
# function, ``_expand_dynamic_allowlist``, that walked the live tree at
# import time and MANUFACTURED an allowlist entry for every writer it
# found in two named test files. An allowlist derived from the files it
# checks cannot report anything in them: for those paths the allowlist WAS
# the scan result, and ``violations()`` compared the scan against itself.
# Measured (elspeth-7eac6c2e24): planting an unreviewed
# ``composition_states_table.insert()`` in one of the two covered files
# left the gate GREEN, while the identical writer in any other file was
# caught. The scanner was never the problem.
#
# The only surviving constraint was a count of found sites against a
# hard-coded tuple of "expected lines" — and those line numbers were never
# compared to anything, only printed. They had drifted by thousands of
# lines (declared 318/377/441..., real 813/872/936...) while the gate
# reported no drift at all. Worse, the AssertionError raised when the
# count moved told the maintainer to update the number, which blesses
# whatever is in the file at that moment. The gate's own failure message
# prescribed the laundering.
#
# So: if you find yourself writing a loop that reads the tree and appends
# ReviewedWriter entries, you are re-introducing the defect. The reviewed
# inventory must be an authority the gate does NOT derive from the tree it
# is checking. Regenerate candidates in a scratch script if you like, but
# paste the result here, so that adding a writer shows up as a diff a human
# reads and approves. The line numbers in ``purpose`` are navigation aids
# only; nothing keys on them, so they may go stale without lying.
#
# Every entry below is a TEST-side write. Production writers live in
# ``_REVIEWED_ALLOWLIST`` above.

_TEST_FIXTURE_REVIEWED_WRITERS: tuple[ReviewedWriter, ...] = (
    # ------ FK-parent setup rows: a run needs a composition_state ------
    # ``runs.composition_state_id`` is a FK to ``composition_states``, so a
    # test that needs a live/completed run to exercise a blob guard has to
    # create the parent state row first. These are fixture scaffolding for
    # the guard under test, not transcript writes: no sequence_no, no lock
    # discipline, and nothing reads them back as audit evidence.
    ReviewedWriter(
        path="tests/unit/web/blobs/test_service.py",
        enclosing_symbol="TestDeleteBlob.test_delete_blob_rejects_when_active_run_linked",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="blobs test: composition_states parent row for the runs FK, so the active-run delete guard has a live run to reject (line 813)",
    ),
    ReviewedWriter(
        path="tests/unit/web/blobs/test_service.py",
        enclosing_symbol="TestDeleteBlob.test_delete_blob_allows_when_completed_run_linked",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="blobs test: composition_states parent row for the runs FK, so the guard sees a COMPLETED linked run and allows the delete (line 872)",
    ),
    ReviewedWriter(
        path="tests/unit/web/blobs/test_service.py",
        enclosing_symbol="TestDeleteBlob.test_delete_blob_preserves_completed_inline_resolution_audit_rows",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="blobs test: composition_states parent row for the runs FK behind the completed-run inline-resolution audit rows under test (line 936)",
    ),
    ReviewedWriter(
        path="tests/unit/web/blobs/test_service.py",
        enclosing_symbol="TestDeleteBlob.test_delete_blob_allows_when_completed_run_exists_without_link",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="blobs test: composition_states parent row for the runs FK, for the completed-run-but-unlinked branch of the delete guard (line 1154)",
    ),
    ReviewedWriter(
        path="tests/unit/web/blobs/test_service.py",
        enclosing_symbol="TestFinalizeRunOutputBlobs.run_env",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="blobs test env fixture: composition_states parent row for the run whose output blobs are finalized (line 4595)",
    ),
    ReviewedWriter(
        path="tests/unit/web/blobs/test_service.py",
        enclosing_symbol="TestFinalizeRunOutputBlobsPartialFailure.run_env",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="blobs test env fixture: composition_states parent row for the partial-failure finalize scenario (line 4750)",
    ),
    ReviewedWriter(
        path="tests/unit/web/blobs/test_service.py",
        enclosing_symbol="TestFinalizeRunOutputBlobsErrorCleanup.run_env",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="blobs test env fixture: composition_states parent row for the finalize error-cleanup scenario (line 5490)",
    ),
    ReviewedWriter(
        path="tests/unit/web/blobs/test_service.py",
        enclosing_symbol="TestLinkBlobToRunDirectionGuard._make_run",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="blobs test helper: composition_states parent row for each run built by the link-direction guard test (line 5858)",
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_tools.py",
        enclosing_symbol="TestDeleteBlobActiveRunGuard._insert_run_and_link",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="composer tool test helper: composition_states parent row for the runs FK behind a blob-linked run (line 5657)",
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_tools.py",
        enclosing_symbol="TestDeleteBlobActiveRunGuard._insert_run_without_link",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="composer tool test helper: composition_states parent row for the runs FK behind an unlinked run (line 5732)",
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_tools.py",
        enclosing_symbol="TestUpdateBlobActiveRunGuard._insert_run_and_link",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="composer tool test helper: composition_states parent row for the runs FK behind a blob-linked run, update-guard case (line 15534)",
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_tools.py",
        enclosing_symbol="TestUpdateBlobActiveRunGuard._insert_run_without_link",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="composer tool test helper: composition_states parent row for the runs FK behind an unlinked run, update-guard case (line 15599)",
    ),
    ReviewedWriter(
        path="tests/unit/web/composer/test_tools.py",
        enclosing_symbol="TestUpdateBlobAtomicWrite.test_guard_rejection_leaves_storage_untouched_and_no_tempfile",
        table="composition_states",
        operation="sqlalchemy_table_insert",
        purpose="composer tool test: composition_states parent row for the pending run that forces the update guard to reject (line 15992)",
    ),
    # ------ rev-4 chat_messages schema tests ------
    # These prove the DATABASE enforces the rev-4 CHECK and partial-UNIQUE
    # constraints, not that SQLAlchemy metadata declares them (the file's
    # own docstring: schema-only introspection would pass against any
    # declared schema). A constraint can only be proven by presenting the
    # offending row to the engine, so the write must be direct: routing it
    # through ``_insert_chat_message`` would have the helper reject or
    # normalise the value before the database ever sees it, and the test
    # would pass without the constraint existing.
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_role_tool_requires_tool_call_id",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: role='tool' with tool_call_id NULL must trip ck_chat_messages_tool_call_id_role (line 34)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_role_assistant_rejects_tool_call_id",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: role='assistant' carrying a tool_call_id must trip ck_chat_messages_tool_call_id_role (line 53)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_writer_principal_check_rejects_unknown",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: an unknown writer_principal must be rejected by the CHECK constraint (line 71)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_writer_principal_check_accepts_run_diagnostics",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: the run_diagnostics writer_principal added by elspeth-0fcf68d50f must be accepted by the CHECK constraint (line 90)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_audit_role_allows_unparented_internal_breadcrumb",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: role='audit' with no parent_assistant_id must be accepted (internal composer breadcrumb) (line 113)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_parent_role_tool_with_parent_id_set_is_accepted",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: parent-role biconditional, accepted branch — one assistant parent row plus the tool row under test (lines 143, 154)",
        count=2,
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_parent_role_tool_without_parent_id_rejected",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: parent-role biconditional — role='tool' with a NULL parent_assistant_id must be rejected (line 176)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_parent_role_non_tool_with_parent_id_rejected",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: parent-role biconditional — one assistant parent row, then a non-tool row naming it must be rejected (lines 197, 209)",
        count=2,
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_parent_role_non_tool_without_parent_id_is_accepted",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: parent-role biconditional — a non-tool row with no parent_assistant_id must be accepted (line 230)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_session_sequence_no_unique",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: two rows reusing one (session_id, sequence_no) must trip the unique constraint — the collision needs both writes (lines 248, 263)",
        count=2,
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_direct_delete_assistant_row_is_blocked_and_session_cascade_purges_tools",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: user + assistant + tool transcript seeded directly so the delete-block and session-cascade behaviour can be observed (lines 303, 316, 329)",
        count=3,
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_tool_row_rejects_cross_session_parent_assistant",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: assistant row in one session, then a tool row in another naming it — the cross-session parent must be rejected (lines 389, 404)",
        count=2,
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_tool_call_id_unique_within_session",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: assistant parent plus two tool rows sharing one tool_call_id in one session must trip the partial unique index (lines 433, 444, 467)",
        count=3,
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_tool_call_id_may_repeat_across_sessions",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: the unique index is scoped to (session_id, tool_call_id), so the same tool_call_id in two sessions must be accepted (lines 493, 504)",
        count=2,
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_chat_messages.py",
        enclosing_symbol="test_tool_call_id_unique_only_within_role_tool",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="schema test: the partial index excludes role!='tool' rows, so a non-tool row may reuse a tool_call_id (lines 524, 536)",
        count=2,
    ),
    # ------ rev-4 composition_states.provenance schema tests ------
    # Same rationale as the chat_messages schema tests above: the CHECK and
    # NOT NULL constraints on ``provenance`` can only be proven by handing
    # the database a row it must refuse.
    ReviewedWriter(
        path="tests/unit/web/sessions/test_composition_states.py",
        enclosing_symbol="test_provenance_check_accepts_known_values",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose="schema test: every declared provenance value must be accepted by ck_composition_states_provenance (line 44)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_composition_states.py",
        enclosing_symbol="test_provenance_check_rejects_unknown_value",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose="schema test: an undeclared provenance value must be rejected by ck_composition_states_provenance (line 62)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_composition_states.py",
        enclosing_symbol="test_provenance_not_null",
        table="composition_states",
        operation="sqlalchemy_insert_call",
        purpose="schema test: a NULL provenance must be rejected by the NOT NULL constraint (line 80)",
    ),
    # ------ lock and sequence-allocator tests ------
    # These test ``_session_write_lock`` and ``_reserve_sequence_range``
    # themselves. Routing their writes through ``_insert_chat_message``
    # would put the component under test inside its own fixture.
    ReviewedWriter(
        path="tests/unit/web/sessions/test_persist_compose_turn.py",
        enclosing_symbol="test_reserve_sequence_range_continues_after_existing",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="allocator test: seeds a row at a known sequence_no so the allocator must continue after it rather than restart at 1 (line 201)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_persist_compose_turn.py",
        enclosing_symbol="test_session_write_lock_serializes_sqlite_same_session_sequence_allocation._writer",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="lock test: the racing writer body writes inside _session_write_lock with a sequence_no it allocated, proving serialization end to end (line 255)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_persist_compose_turn.py",
        enclosing_symbol="test_file_backed_sqlite_lock_serializes_independent_connections._writer",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="lock test: same racing-writer body against a file-backed SQLite database and independent connections (line 340)",
    ),
    ReviewedWriter(
        path="tests/unit/web/sessions/test_persist_compose_turn.py",
        enclosing_symbol="test_insert_chat_message_rejects_tool_parent_that_is_not_assistant",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="helper test: seeds a NON-assistant row directly so _insert_chat_message has an invalid tool parent to refuse (line 546)",
    ),
    # ------ shared transcript fixtures ------
    ReviewedWriter(
        path="tests/unit/web/conftest.py",
        enclosing_symbol="session_with_user_assistant_tool_rows",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="shared unit-web fixture: seeds a user/assistant/tool transcript with fixed sequence_no and writer_principal values for reader-side tests (line 204)",
    ),
    ReviewedWriter(
        path="tests/integration/web/conftest.py",
        enclosing_symbol="session_with_pending_compose_request",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="shared integration-web fixture: seeds the single user message a pending compose request answers (line 132)",
    ),
    ReviewedWriter(
        path="tests/integration/pipeline/test_composer_runtime_agreement.py",
        enclosing_symbol="TestCsvBindGuaranteeRuntimeAgreement._bind_csv_blob_state",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose=(
            "runtime-agreement fixture: seeds the single user message that the REAL "
            "bind tool answers, so the CSV guarantee stamp comes from production code; "
            "written via `from sqlalchemy import insert as _insert` (line 7054). "
            "Invisible to this gate until the callable was resolved from imports "
            "rather than matched by spelling — the one live writer that widening surfaced."
        ),
    ),
)

_ALLOWLIST: tuple[ReviewedWriter, ...] = _REVIEWED_ALLOWLIST + _TEST_FIXTURE_REVIEWED_WRITERS


def _format_violations(
    direct: Sequence[WriterMatch],
    lock: Sequence[LockDisciplineViolation],
    helper_assert: Sequence[LockDisciplineViolation],
    inline: Sequence[InlineAllocViolation],
    stale: Sequence[StaleReviewedWriter] = (),
) -> str:
    """Render a human-readable failure report for the five check kinds."""

    lines = []
    if direct:
        lines.append("Unallowlisted direct writer sites:")
        for m in direct:
            lines.append(f"  {m.path}:{m.line} [{m.table}/{m.operation}] in {m.enclosing_symbol}")
            lines.append(f"      {m.snippet}")
    if stale:
        lines.append("Reviewed writer sites the tree no longer contains:")
        for s in stale:
            lines.append(
                f"  {s.entry.path} [{s.entry.table}/{s.entry.operation}] in {s.entry.enclosing_symbol}: "
                f"reviewed count={s.entry.count}, found {s.found}"
            )
            lines.append(f"      {s.entry.purpose}")
    if lock:
        lines.append("Lock-required helper called outside _session_write_lock:")
        for lock_v in lock:
            lines.append(f"  {lock_v.path}:{lock_v.line} [{lock_v.helper_name}] in {lock_v.enclosing_symbol}")
            lines.append(f"      {lock_v.snippet}")
    if helper_assert:
        lines.append("Lock helper missing _assert_session_write_lock_held call:")
        for assert_v in helper_assert:
            lines.append(f"  {assert_v.path}:{assert_v.line} [{assert_v.helper_name}] in {assert_v.enclosing_symbol}")
            lines.append(f"      {assert_v.snippet}")
    if inline:
        lines.append("Inline composition_states.version allocation outside _session_write_lock:")
        for inline_v in inline:
            lines.append(f"  {inline_v.path}:{inline_v.line} in {inline_v.enclosing_symbol}")
            lines.append(f"      {inline_v.snippet}")
    return "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------
# Required guard tests
# ---------------------------------------------------------------------------


def test_static_direct_writers_match_reviewed_allowlist() -> None:
    """The live ``src/`` and ``tests/`` tree matches the reviewed writer inventory.

    This is the merge gate for the Schedule 1A schema/current-writer
    cutover. It scans every Python file under ``src/`` and ``tests/``
    (skipping this scanner module) and fails on drift in either
    direction:

    * a writer site with no reviewed entry, or one write more of a
      reviewed shape than the review blessed (:func:`violations`); and
    * a reviewed entry with fewer live writes than it claims — a removed
      writer or a renamed enclosing function
      (:func:`stale_reviewed_writers`).

    Both halves compare the tree against the literal inventory committed
    in this file. Nothing in the allowlist is derived from the tree it
    checks; see the comment above ``_TEST_FIXTURE_REVIEWED_WRITERS``.
    """

    repo_root = _find_repo_root()
    matches = scan_writers(
        [repo_root / "src", repo_root / "tests"],
        path_anchor=repo_root,
    )
    direct = violations(matches, _ALLOWLIST)
    stale = stale_reviewed_writers(matches, _ALLOWLIST)
    lock = check_lock_discipline(
        [repo_root / "src", repo_root / "tests"],
        path_anchor=repo_root,
        allowlist=_LOCK_DISCIPLINE_NEGATIVE_TESTS,
    )
    helper_assert = check_helper_lock_assertions(
        [repo_root / "src", repo_root / "tests"],
        path_anchor=repo_root,
    )
    inline = check_inline_state_version_allocation(
        [repo_root / "src", repo_root / "tests"],
        path_anchor=repo_root,
    )
    report = _format_violations(direct, lock, helper_assert, inline, stale)
    assert not report, (
        "Static direct-writer guard found unreviewed sites, a surplus write inside a reviewed\n"
        "site, a reviewed site that no longer exists, or lock-discipline drift.\n"
        "If a new writer/helper-call is intentional, add a justified entry to\n"
        "_REVIEWED_ALLOWLIST / _TEST_FIXTURE_REVIEWED_WRITERS (or raise an existing entry's\n"
        "count=N), and update the inventory table in the cutover PR body. If a writer was\n"
        "deleted, delete its allowlist entry in the same commit. Never widen an entry to make\n"
        "this pass without reading the write it now blesses.\n\n"
        f"{report}"
    )


def test_static_direct_writer_guard_rejects_unreviewed_chat_insert(tmp_path: Path) -> None:
    """The scanner fail-closes against a synthetic unallowlisted ``chat_messages`` writer.

    Writes a synthetic test file under ``tmp_path`` containing a
    ``chat_messages_table.insert(...)`` call, scans it, and asserts the
    scanner reports the violation against the reviewed allowlist.
    """

    synthetic_root = tmp_path / "tests"
    synthetic_root.mkdir()
    synthetic = synthetic_root / "test_synthetic_chat_writer.py"
    synthetic.write_text(
        textwrap.dedent("""\
        from elspeth.web.sessions.models import chat_messages_table
        from sqlalchemy import insert


        def test_synthetic_unallowlisted_writer(engine):
            with engine.begin() as conn:
                conn.execute(insert(chat_messages_table).values(id="X"))
    """)
    )
    matches = scan_writers([synthetic_root], path_anchor=tmp_path)
    unallowed = violations(matches, _REVIEWED_ALLOWLIST)
    assert any(m.table == "chat_messages" for m in unallowed), (
        f"scanner failed to detect synthetic unallowlisted chat_messages insert; matches={matches} unallowed={unallowed}"
    )
    assert any("test_synthetic_chat_writer.py" in m.path for m in unallowed)


def test_static_direct_writer_guard_rejects_unreviewed_state_insert(tmp_path: Path) -> None:
    """The scanner fail-closes against a synthetic unallowlisted ``composition_states`` writer."""

    synthetic_root = tmp_path / "tests"
    synthetic_root.mkdir()
    synthetic = synthetic_root / "test_synthetic_state_writer.py"
    synthetic.write_text(
        textwrap.dedent("""\
        from elspeth.web.sessions.models import composition_states_table


        def test_synthetic_unallowlisted_state(engine):
            with engine.begin() as conn:
                conn.execute(composition_states_table.insert().values(id="X"))
    """)
    )
    matches = scan_writers([synthetic_root], path_anchor=tmp_path)
    unallowed = violations(matches, _REVIEWED_ALLOWLIST)
    assert any(m.table == "composition_states" for m in unallowed), (
        f"scanner failed to detect synthetic unallowlisted composition_states insert; matches={matches} unallowed={unallowed}"
    )
    assert any("test_synthetic_state_writer.py" in m.path for m in unallowed)


def test_scanner_sees_qualified_table_references(tmp_path: Path) -> None:
    """A writer reached through a qualified import is not invisible.

    Regression for elspeth-9b3cf0d52d. The scanner used to accept only a
    bare ``ast.Name`` for the table, so ``insert(models.chat_messages_table)``
    and ``models.composition_states_table.insert()`` produced no match at
    all — a writer spelled that way was outside the gate entirely, not
    merely unreviewed. Measured before the fix: two such writers in a file
    the allowlist had never heard of left the whole gate green.

    The CALLABLE may be qualified too. While Pattern 1 required
    ``isinstance(func, ast.Name)``, ``sa.insert(chat_messages_table)``
    produced no match at all — the same "outside the gate entirely"
    failure, on the import rather than the argument. That case is pinned
    below because it is one keystroke from live: measured on this tree,
    ``import sqlalchemy as sa`` appears in nine files besides this one
    (including ``src/elspeth/web/secrets/user_store.py``), backing six
    ``sa.select``/``sa.update`` call sites.

    The callable may also be ALIASED at import. Matching the spelling
    ``insert`` (the first widening) still let ``from
    sqlalchemy.dialects.sqlite import insert as sqlite_insert;
    sqlite_insert(chat_messages_table)`` through with no match at all —
    and that spelling is live house style for upserts in this very
    subsystem (``user_store.py`` binds it three times). The scanner now
    resolves the callable from the file's imports, and that case is
    pinned below too.

    Every spelling must also classify to the SAME table and operation as
    its bare equivalent, otherwise an author could still slip a second
    write past a reviewed key by switching import style.
    """

    synthetic_root = tmp_path / "tests"
    synthetic_root.mkdir()
    (synthetic_root / "test_synthetic_qualified_writer.py").write_text(
        textwrap.dedent("""\
        import sqlalchemy as sa
        from sqlalchemy import insert

        from elspeth.web.sessions import models


        def test_synthetic_qualified(engine):
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            with engine.begin() as conn:
                conn.execute(insert(models.chat_messages_table).values(id="X"))
                conn.execute(models.composition_states_table.insert().values(id="Y"))
                conn.execute(sa.insert(models.composition_states_table).values(id="Z"))
                conn.execute(sqlite_insert(models.chat_messages_table).values(id="W").on_conflict_do_nothing())
    """)
    )
    matches = scan_writers([synthetic_root], path_anchor=tmp_path)
    found = {(m.table, m.operation) for m in matches}
    aliased_callable_writes = [m for m in matches if "sqlite_insert(" in m.snippet]
    assert [(m.table, m.operation) for m in aliased_callable_writes] == [("chat_messages", "sqlalchemy_insert_call")], (
        f"import-aliased callable sqlite_insert(models.chat_messages_table) not detected, or detected on a "
        f"different key from the bare spelling; matches={matches}"
    )
    assert ("chat_messages", "sqlalchemy_insert_call") in found, (
        f"qualified insert(models.chat_messages_table) not detected; matches={matches}"
    )
    assert ("composition_states", "sqlalchemy_table_insert") in found, (
        f"qualified models.composition_states_table.insert() not detected; matches={matches}"
    )
    assert ("composition_states", "sqlalchemy_insert_call") in found, (
        f"qualified-callable sa.insert(models.composition_states_table) not detected; matches={matches}"
    )
    assert len(violations(matches, _ALLOWLIST)) == 4, (
        f"qualified writers must be reported against the reviewed allowlist; matches={matches}"
    )


def test_second_write_inside_a_reviewed_site_is_reported(tmp_path: Path) -> None:
    """One reviewed write does not bless a second one beside it.

    Regression for elspeth-7eac6c2e24(b). The allowlist key was a set of
    ``(path, enclosing_symbol, table, operation)`` tuples, which cannot
    express "exactly one reviewed write here". Measured before the fix on
    a real production site: a second ``insert(chat_messages_table)`` added
    inside ``SessionServiceImpl.fork_session._sync`` — already allowlisted
    for that exact 4-tuple — reproduced the key and passed the gate, while
    the same write spelled ``chat_messages_table.insert()`` was caught,
    because that spelling lands on a different ``operation``. The gate
    caught the stylistically inconsistent addition and missed the
    consistent one, which is the one a developer actually writes.

    The reported surplus must be the LATER line: the first write is the
    reviewed one.
    """

    synthetic_root = tmp_path / "tests"
    synthetic_root.mkdir()
    (synthetic_root / "test_reviewed_site.py").write_text(
        textwrap.dedent("""\
        from sqlalchemy import insert

        from elspeth.web.sessions.models import chat_messages_table


        def reviewed_writer(conn):
            conn.execute(insert(chat_messages_table).values(id="reviewed"))
            conn.execute(insert(chat_messages_table).values(id="surplus"))
    """)
    )
    allowlist = (
        ReviewedWriter(
            path="tests/test_reviewed_site.py",
            enclosing_symbol="reviewed_writer",
            table="chat_messages",
            operation="sqlalchemy_insert_call",
            purpose="synthetic: exactly one reviewed write",
        ),
    )
    matches = scan_writers([synthetic_root], path_anchor=tmp_path)
    assert len(matches) == 2, f"expected both writes to be scanned; matches={matches}"

    surplus = violations(matches, allowlist)
    assert len(surplus) == 1, f"the second write must be reported as surplus; surplus={surplus}"
    assert surplus[0].snippet.endswith('id="surplus"))'), (
        f"the surplus reported must be the later write, not the reviewed one; got {surplus[0]}"
    )
    assert not stale_reviewed_writers(matches, allowlist)

    # Raising the reviewed count to 2 is the deliberate, diff-visible way to
    # bless the second write — and it must then bless exactly two, not more.
    widened = (replace(allowlist[0], count=2),)
    assert not violations(matches, widened)
    assert not stale_reviewed_writers(matches, widened)
    assert stale_reviewed_writers(matches[:1], widened), "an entry claiming more writes than exist must be reported stale"


def test_reviewed_entry_without_a_live_writer_is_reported_stale() -> None:
    """A reviewed entry that outlives its writer is drift, not silence.

    The removal half of the claim this module makes about drift. Before
    the count key there was nowhere to detect it: an entry whose writer
    had been deleted simply never matched anything, and the gate stayed
    green while vouching for code that was gone. Four such entries were
    found in this file when the check was added.
    """

    entry = ReviewedWriter(
        path="tests/does_not_exist.py",
        enclosing_symbol="vanished",
        table="chat_messages",
        operation="sqlalchemy_insert_call",
        purpose="synthetic: writer has been deleted",
    )
    stale = stale_reviewed_writers([], (entry,))
    assert [(s.entry.enclosing_symbol, s.found) for s in stale] == [("vanished", 0)]


def test_duplicate_reviewed_entries_are_rejected() -> None:
    """Two entries with one key must raise, not silently collapse.

    ``reviewed_counts`` is built with an explicit duplicate check because
    the obvious dict/set comprehension keeps only the last entry. That is
    not hypothetical: this file carried two entries for
    ``TestCompositionStateUniqueConstraint.test_duplicate_version_raises``
    describing two deliberate rows, and the set key merged them, so the
    review's own multiplicity claim was discarded on load.
    """

    duplicated = (
        ReviewedWriter(
            path="tests/dup.py",
            enclosing_symbol="writer",
            table="chat_messages",
            operation="sqlalchemy_insert_call",
            purpose="first",
        ),
        ReviewedWriter(
            path="tests/dup.py",
            enclosing_symbol="writer",
            table="chat_messages",
            operation="sqlalchemy_insert_call",
            purpose="second",
        ),
    )
    with pytest.raises(AssertionError, match="duplicate reviewed-writer entry"):
        reviewed_counts(duplicated)


def _import_time_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Yield the nodes of ``node`` that execute when the module is imported.

    Like :func:`ast.walk` minus the bodies of ``def`` / ``async def``,
    which run only when called. Everything else does run at import: module
    statements, CLASS bodies, decorators, and default-argument
    expressions. Pruning at the ``FunctionDef`` node itself — the obvious
    shortcut — would drop those last three with it.
    """

    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(child is stmt for stmt in node.body):
            continue
        yield from _import_time_nodes(child)


def test_reviewed_inventory_is_a_committed_literal() -> None:
    """No allowlist entry may be manufactured from the tree under review.

    This is the tripwire for elspeth-7eac6c2e24(a). The predecessor of
    ``_TEST_FIXTURE_REVIEWED_WRITERS`` was a function that scanned two
    named test files at import time and built a ReviewedWriter for every
    writer it found, so for those paths the allowlist was the scan result
    and ``violations()`` compared the tree against itself: an unreviewed
    writer planted in either file left the gate green, while the identical
    writer anywhere else was caught.

    Re-automating that is a one-line change and reads like a tidy-up, so
    the shape is pinned here rather than left to the reviewer's memory.
    Exactly two things are checked, and no more:

    1. Each of the three module-level allowlist definitions — the two
       source tuples AND ``_ALLOWLIST``, which is the one
       :func:`violations` actually consumes — contains no call other
       than ``ReviewedWriter``. ``_ALLOWLIST`` is checked because that is
       where a third, tree-derived term would be cheapest to fold in;
       omitting it left the consumed tuple unguarded.
    2. Nothing that executes at import calls one of the four tree-readers
       this module has to hand — ``scan_writers``, ``_iter_python_files``,
       ``iter_gate_sources``, ``read_text`` — which is the mechanism the deleted
       ``_expand_dynamic_allowlist`` used. "Executes at import" is
       :func:`_import_time_nodes`: everything except ``def`` bodies, so
       class bodies, decorators and argument defaults are covered while
       this very test's own ``read_text`` call is not. A tree-reader
       reached under some fourth name is not caught.

    NOT checked, and MEASURED not to be: the alias hop. A module-level
    helper that wraps the tree-read (``def _hop(): return
    scan_writers(...)``) bound to an intermediate name (``_DERIVED =
    _hop()``) and folded in as ``_ALLOWLIST = ... + _DERIVED`` passes BOTH
    clauses — clause 1 sees only the Name ``_DERIVED``, and clause 2 sees
    only the Call ``_hop``, whose tree-read is inside a ``def`` body it
    prunes. Closing that needs dataflow analysis, which is not built here.
    What remains is a reviewer's job: an intermediate name feeding the
    allowlist is the shape to reject on sight.
    """

    assert isinstance(_ALLOWLIST, tuple)
    assert all(isinstance(entry, ReviewedWriter) for entry in _ALLOWLIST)

    module = ast.parse(_SCANNER_SELF_PATH.read_text(encoding="utf-8"))
    literal_targets = {"_REVIEWED_ALLOWLIST", "_TEST_FIXTURE_REVIEWED_WRITERS", "_ALLOWLIST"}
    seen: set[str] = set()
    for node in module.body:
        # ``AugAssign`` (``_ALLOWLIST += _hop()``) and a tuple target
        # (``_ALLOWLIST, _ = _hop(), None``) are redefinitions of the same
        # name and are inspected like any other; measured before they were
        # included, either one re-derived the allowlist with the file green.
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        flat_targets = [elt for t in targets for elt in (t.elts if isinstance(t, ast.Tuple) else [t])]
        names = {t.id for t in flat_targets if isinstance(t, ast.Name)}
        for name in names & literal_targets:
            seen.add(name)
            value = node.value
            assert value is not None
            calls = [
                _call_callable_name(call)
                for call in ast.walk(value)
                if isinstance(call, ast.Call) and _call_callable_name(call) != "ReviewedWriter"
            ]
            assert not calls, (
                f"{name} must be a committed literal, but its definition calls {sorted(set(calls))}. "
                f"An allowlist derived from the tree it checks cannot report anything in that tree "
                f"(elspeth-7eac6c2e24). Generate candidates in a scratch script and paste the result."
            )
    assert seen == literal_targets, f"expected literal allowlist definitions for {sorted(literal_targets)}, found {sorted(seen)}"

    tree_readers = {"scan_writers", "_iter_python_files", "iter_gate_sources", "read_text"}
    import_time_reads = sorted(
        {
            name
            for name in (_call_callable_name(node) for node in _import_time_nodes(module) if isinstance(node, ast.Call))
            if name in tree_readers
        }
    )
    assert not import_time_reads, (
        f"this module reads the tree at import time via {import_time_reads}. "
        f"An allowlist assembled from a tree-read at import cannot report anything in that "
        f"tree (elspeth-7eac6c2e24) — that is exactly what _expand_dynamic_allowlist did. "
        f"Read the tree inside a function body, never anywhere that runs at import."
    )


def test_static_helper_lock_guard_rejects_unlocked_allocator(tmp_path: Path) -> None:
    """The lock-discipline checker fail-closes against a synthetic unlocked helper call.

    Writes a synthetic source file that defines ``_session_write_lock``
    (so the conditional-dormancy gate opens), then calls
    ``_insert_chat_message`` outside any ``with`` block. Asserts the
    checker reports the violation.

    Also asserts that adding the proper ``with self._session_write_lock(...):``
    wrapper around the same call removes the violation, so the checker
    is not over-triggering.
    """

    synthetic_root = tmp_path / "src"
    synthetic_root.mkdir()
    synthetic = synthetic_root / "synthetic_module.py"
    synthetic.write_text(
        textwrap.dedent("""\
        from contextlib import contextmanager


        @contextmanager
        def _session_write_lock(conn, sid):
            yield


        def _insert_chat_message(conn, *, session_id):
            pass


        def _assert_session_write_lock_held(conn, *, caller):
            pass


        class Service:
            def use_helper_unlocked(self, conn, sid):
                _insert_chat_message(conn, session_id=sid)

            def use_helper_locked(self, conn, sid):
                with _session_write_lock(conn, sid):
                    _insert_chat_message(conn, session_id=sid)
    """)
    )
    findings = check_lock_discipline([synthetic_root], path_anchor=tmp_path)
    unlocked = [v for v in findings if v.enclosing_symbol.endswith("use_helper_unlocked")]
    locked = [v for v in findings if v.enclosing_symbol.endswith("use_helper_locked")]
    assert unlocked, f"lock-discipline checker failed to detect helper call outside _session_write_lock; findings={findings}"
    assert not locked, f"lock-discipline checker over-triggered on properly-locked call site; locked-findings={locked}"


def test_lock_assertion_must_dominate_and_match_the_writer_arguments(tmp_path: Path) -> None:
    """A decorative, unreachable, late, or differently-bound assertion grants no exemption."""

    synthetic_root = tmp_path / "src"
    synthetic_root.mkdir()
    (synthetic_root / "synthetic_module.py").write_text(
        textwrap.dedent("""\
        from contextlib import contextmanager

        @contextmanager
        def _session_write_lock(conn, sid):
            yield

        def _assert_session_write_lock_held(conn, sid, *, caller):
            pass

        def _insert_chat_message(conn, *, session_id):
            pass

        def valid(conn, sid):
            _assert_session_write_lock_held(conn, sid, caller="valid")
            _insert_chat_message(conn, session_id=sid)

        def assertion_after_write(conn, sid):
            _insert_chat_message(conn, session_id=sid)
            _assert_session_write_lock_held(conn, sid, caller="late")

        def unreachable_assertion(conn, sid):
            if False:
                _assert_session_write_lock_held(conn, sid, caller="unreachable")
            _insert_chat_message(conn, session_id=sid)

        def wrong_connection(conn, other_conn, sid):
            _assert_session_write_lock_held(other_conn, sid, caller="wrong conn")
            _insert_chat_message(conn, session_id=sid)

        def wrong_session(conn, sid, other_sid):
            _assert_session_write_lock_held(conn, other_sid, caller="wrong session")
            _insert_chat_message(conn, session_id=sid)
    """)
    )

    findings = check_lock_discipline([synthetic_root], path_anchor=tmp_path)
    symbols = {finding.enclosing_symbol for finding in findings}
    assert "valid" not in symbols
    assert {
        "assertion_after_write",
        "unreachable_assertion",
        "wrong_connection",
        "wrong_session",
    } <= symbols


def test_lock_context_must_match_writer_connection_and_session_member(tmp_path: Path) -> None:
    """A named lock context protects only its exact connection and session member."""

    synthetic_root = tmp_path / "src"
    synthetic_root.mkdir()
    (synthetic_root / "synthetic_module.py").write_text(
        textwrap.dedent("""\
        from contextlib import contextmanager

        @contextmanager
        def _session_write_lock(conn, session_id):
            yield

        @contextmanager
        def _session_pair_locked_begin(first_session_id, second_session_id):
            yield object()

        def _insert_chat_message(conn, *, session_id):
            pass

        def valid_single(conn, sid):
            with _session_write_lock(conn, sid):
                _insert_chat_message(conn, session_id=sid)

        def wrong_single_connection(conn, other_conn, sid):
            with _session_write_lock(other_conn, sid):
                _insert_chat_message(conn, session_id=sid)

        def wrong_single_session(conn, sid, other_sid):
            with _session_write_lock(conn, other_sid):
                _insert_chat_message(conn, session_id=sid)

        def valid_pair_first(first_sid, second_sid):
            with _session_pair_locked_begin(first_sid, second_sid) as conn:
                _insert_chat_message(conn, session_id=first_sid)

        def valid_pair_second(first_sid, second_sid):
            with _session_pair_locked_begin(first_sid, second_sid) as conn:
                _insert_chat_message(conn, session_id=second_sid)

        def wrong_pair_connection(other_conn, first_sid, second_sid):
            with _session_pair_locked_begin(first_sid, second_sid) as conn:
                _insert_chat_message(other_conn, session_id=first_sid)

        def wrong_pair_member(first_sid, second_sid, unrelated_sid):
            with _session_pair_locked_begin(first_sid, second_sid) as conn:
                _insert_chat_message(conn, session_id=unrelated_sid)
    """)
    )

    findings = check_lock_discipline([synthetic_root], path_anchor=tmp_path)
    symbols = {finding.enclosing_symbol for finding in findings}
    assert {"valid_single", "valid_pair_first", "valid_pair_second"}.isdisjoint(symbols)
    assert {
        "wrong_single_connection",
        "wrong_single_session",
        "wrong_pair_connection",
        "wrong_pair_member",
    } <= symbols


def test_lock_discipline_allowlist_exempts_negative_precondition_test(tmp_path: Path) -> None:
    """Plan §94: negative-precondition tests must be exempt from the lock-discipline check.

    Synthesises a test file with two helper calls outside any lock:
    one in an allowlisted ``test_<helper>_requires_session_write_lock``
    function, and one in an unrelated function. Asserts the allowlist
    suppresses ONLY the matching site, not the unrelated one. Without
    the allowlist, both calls must be flagged (no false negatives).
    """

    synthetic_src = tmp_path / "src"
    synthetic_src.mkdir()
    (synthetic_src / "synthetic_module.py").write_text(
        textwrap.dedent("""\
        from contextlib import contextmanager


        @contextmanager
        def _session_write_lock(conn, sid):
            yield


        def _reserve_sequence_range(conn, sid, *, count):
            return 1


        def _assert_session_write_lock_held(conn, *, caller):
            pass
    """)
    )
    synthetic_tests = tmp_path / "tests"
    synthetic_tests.mkdir()
    (synthetic_tests / "test_synthetic_helpers.py").write_text(
        textwrap.dedent("""\
        from synthetic_module import _reserve_sequence_range


        def test_reserve_sequence_range_requires_session_write_lock(service):
            # Negative-precondition test: deliberately calls helper outside lock.
            _reserve_sequence_range(service, "s_no_lock", count=1)


        def test_unrelated_thing(service):
            # Not a precondition test; must NOT be exempted.
            _reserve_sequence_range(service, "s_other", count=1)
    """)
    )

    allowlist = (
        LockDisciplineNegativeTest(
            path="tests/test_synthetic_helpers.py",
            enclosing_symbol="test_reserve_sequence_range_requires_session_write_lock",
            helper_name="_reserve_sequence_range",
            purpose="synthetic regression test for the allowlist mechanism",
        ),
    )

    strict_findings = check_lock_discipline([synthetic_src, synthetic_tests], path_anchor=tmp_path)
    strict_symbols = {v.enclosing_symbol for v in strict_findings}
    assert "test_reserve_sequence_range_requires_session_write_lock" in strict_symbols, (
        f"scanner failed to flag the negative-precondition call site without the allowlist; strict_findings={strict_findings}"
    )
    assert "test_unrelated_thing" in strict_symbols, (
        f"scanner failed to flag the unrelated call site without the allowlist; strict_findings={strict_findings}"
    )

    permissive_findings = check_lock_discipline([synthetic_src, synthetic_tests], path_anchor=tmp_path, allowlist=allowlist)
    permissive_symbols = {v.enclosing_symbol for v in permissive_findings}
    assert "test_reserve_sequence_range_requires_session_write_lock" not in permissive_symbols, (
        f"allowlist failed to suppress the matching negative-precondition site; permissive_findings={permissive_findings}"
    )
    assert "test_unrelated_thing" in permissive_symbols, (
        f"allowlist over-suppressed an unrelated site (key mismatch should fail closed); permissive_findings={permissive_findings}"
    )


def test_lock_discipline_allowlist_key_match_is_exact(tmp_path: Path) -> None:
    """Allowlist matching must be exact on (path, enclosing_symbol, helper_name).

    A near-miss on any of the three keys must NOT suppress the violation.
    This guards against accidental over-broad suppression — e.g., an
    allowlist entry for ``_reserve_sequence_range`` in one file leaking
    to a same-name function in another file.
    """

    synthetic_src = tmp_path / "src"
    synthetic_src.mkdir()
    (synthetic_src / "synthetic_module.py").write_text(
        textwrap.dedent("""\
        from contextlib import contextmanager


        @contextmanager
        def _session_write_lock(conn, sid):
            yield


        def _reserve_sequence_range(conn, sid, *, count):
            return 1


        def _assert_session_write_lock_held(conn, *, caller):
            pass
    """)
    )
    synthetic_tests = tmp_path / "tests"
    synthetic_tests.mkdir()
    (synthetic_tests / "test_other_file.py").write_text(
        textwrap.dedent("""\
        from synthetic_module import _reserve_sequence_range


        def test_reserve_sequence_range_requires_session_write_lock(service):
            _reserve_sequence_range(service, "s", count=1)
    """)
    )

    # Allowlist entry references a DIFFERENT path — same symbol/helper.
    mismatched_path = (
        LockDisciplineNegativeTest(
            path="tests/some_other_path.py",
            enclosing_symbol="test_reserve_sequence_range_requires_session_write_lock",
            helper_name="_reserve_sequence_range",
            purpose="path-mismatch regression",
        ),
    )
    findings = check_lock_discipline([synthetic_src, synthetic_tests], path_anchor=tmp_path, allowlist=mismatched_path)
    matching = [v for v in findings if v.enclosing_symbol == "test_reserve_sequence_range_requires_session_write_lock"]
    assert matching, f"allowlist with mismatched path must NOT suppress; findings={findings}"

    # Allowlist entry references a DIFFERENT helper — same path/symbol.
    mismatched_helper = (
        LockDisciplineNegativeTest(
            path="tests/test_other_file.py",
            enclosing_symbol="test_reserve_sequence_range_requires_session_write_lock",
            helper_name="_insert_chat_message",
            purpose="helper-mismatch regression",
        ),
    )
    findings = check_lock_discipline([synthetic_src, synthetic_tests], path_anchor=tmp_path, allowlist=mismatched_helper)
    matching = [v for v in findings if v.enclosing_symbol == "test_reserve_sequence_range_requires_session_write_lock"]
    assert matching, f"allowlist with mismatched helper_name must NOT suppress; findings={findings}"


# ---------------------------------------------------------------------------
# Conditional-dormancy regression: dormant when _session_write_lock is absent
# ---------------------------------------------------------------------------


def test_lock_discipline_dormant_when_session_write_lock_absent(tmp_path: Path) -> None:
    """Lock-discipline checker returns no findings when ``_session_write_lock`` is undefined.

    This proves the dormancy rule: until Task 9 introduces
    ``_session_write_lock`` to the live tree, the lock checks return
    ``[]`` even if a synthetic file calls a helper without a lock. The
    moment Task 9 lands, the previous test fires.
    """

    synthetic_root = tmp_path / "src"
    synthetic_root.mkdir()
    synthetic = synthetic_root / "no_lock_module.py"
    synthetic.write_text(
        textwrap.dedent("""\
        def _insert_chat_message(conn, record):
            pass


        class Service:
            def use_helper(self, conn, record):
                _insert_chat_message(conn, record)
    """)
    )
    findings = check_lock_discipline([synthetic_root], path_anchor=tmp_path)
    assert findings == [], f"lock-discipline checker should be dormant without _session_write_lock; findings={findings}"


# ---------------------------------------------------------------------------
# Inline composition_states.version allocation checker self-tests
# (regression for elspeth-13cadbc73d: arity crash, SQLAlchemy-form
# inertness, and nested-closure scope blindness)
# ---------------------------------------------------------------------------


_INLINE_ALLOC_LOCK_PREAMBLE = """\
from contextlib import contextmanager

from sqlalchemy import func, select

from synthetic_models import composition_states_table


@contextmanager
def _session_write_lock(conn, sid):
    yield


"""


def test_inline_allocation_checker_survives_locked_raw_qualified_site(tmp_path: Path) -> None:
    """A compliant raw-SQL allocation inside the lock must scan clean, not crash.

    Regression for the elspeth-13cadbc73d arity defect: the checker
    called ``_with_block_establishes_session_write_lock(w)`` with one
    argument while the predicate requires ``(with_node, writer)``, so
    the first matched raw-SQL site that WAS inside a ``with`` block —
    the compliant case — raised ``TypeError`` and crashed the gate.
    """

    synthetic_root = tmp_path / "src"
    synthetic_root.mkdir()
    (synthetic_root / "synthetic_service.py").write_text(
        _INLINE_ALLOC_LOCK_PREAMBLE
        + textwrap.dedent("""\
        class LockedQualifiedRawService:
            def save_composition_state(self, conn, sid):
                with _session_write_lock(conn, sid):
                    conn.exec_driver_sql(
                        "SELECT MAX(composition_states.version) FROM composition_states WHERE session_id = ?",
                        (sid,),
                    )
    """)
    )
    findings = check_inline_state_version_allocation([synthetic_root], path_anchor=tmp_path)
    assert findings == [], f"locked qualified raw-SQL allocation must not be flagged (and must not crash); findings={findings}"


def test_inline_allocation_checker_covers_both_forms_and_lock_contexts(tmp_path: Path) -> None:
    """Both allocation forms are detected, and only the unlocked sites are flagged.

    Mirrors the live-tree shapes: ``save_composition_state`` /
    ``set_active_state`` allocate inside a nested ``_sync`` closure
    (so the enclosing symbol ends in ``._sync``, exercising the
    symbol-path scope match), via either the SQLAlchemy
    ``select(func.max(composition_states_table.c.version))`` form or a
    raw ``SELECT COALESCE(MAX(version), 0) + 1 FROM composition_states``
    string. Locked variants must scan clean; unlocked variants must be
    flagged; the ``_insert_composition_state`` helper (whose lock
    precondition is enforced by ``check_helper_lock_assertions``) and a
    docstring quoting the SQL must stay out of scope.
    """

    synthetic_root = tmp_path / "src"
    synthetic_root.mkdir()
    (synthetic_root / "synthetic_service.py").write_text(
        _INLINE_ALLOC_LOCK_PREAMBLE
        + textwrap.dedent("""\
        class LockedSqlalchemyService:
            def save_composition_state(self, sid):
                def _sync():
                    with self._session_process_locked_begin(sid) as conn:
                        with _session_write_lock(conn, sid):
                            conn.execute(
                                select(func.max(composition_states_table.c.version)).where(
                                    composition_states_table.c.session_id == sid
                                )
                            )
                return _sync


        class UnlockedSqlalchemyService:
            def save_composition_state(self, sid):
                def _sync():
                    with self._session_process_locked_begin(sid) as conn:
                        conn.execute(
                            select(func.max(composition_states_table.c.version)).where(
                                composition_states_table.c.session_id == sid
                            )
                        )
                return _sync


        class LockedRawService:
            def set_active_state(self, sid):
                def _sync():
                    with self._session_process_locked_begin(sid) as conn:
                        with _session_write_lock(conn, sid):
                            conn.exec_driver_sql(
                                "SELECT COALESCE(MAX(version), 0) + 1 FROM composition_states WHERE session_id = ?",
                                (sid,),
                            )
                return _sync


        class UnlockedRawService:
            def set_active_state(self, sid):
                def _sync():
                    with self._session_process_locked_begin(sid) as conn:
                        conn.exec_driver_sql(
                            "SELECT COALESCE(MAX(version), 0) + 1 FROM composition_states WHERE session_id = ?",
                            (sid,),
                        )
                return _sync


        class DocstringService:
            def save_composition_state(self, sid):
                '''Allocates via SELECT COALESCE(MAX(version), 0) + 1 FROM composition_states.'''
                return None


        def _insert_composition_state(conn, sid):
            conn.execute(
                select(func.max(composition_states_table.c.version)).where(
                    composition_states_table.c.session_id == sid
                )
            )
    """)
    )
    findings = check_inline_state_version_allocation([synthetic_root], path_anchor=tmp_path)
    symbols = {finding.enclosing_symbol for finding in findings}
    assert symbols == {
        "UnlockedSqlalchemyService.save_composition_state._sync",
        "UnlockedRawService.set_active_state._sync",
    }, (
        "inline-allocation checker must flag exactly the unlocked SQLAlchemy-form and "
        f"raw-form allocation sites (and nothing else); findings={findings}"
    )
