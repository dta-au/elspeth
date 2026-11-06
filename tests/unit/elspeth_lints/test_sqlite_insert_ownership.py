"""Normal current-rule regressions for per-writer dialect ownership."""

from __future__ import annotations

import ast
import textwrap

import pytest
from tests.unit.elspeth_lints.test_portable_sqlite_insert_rule import _analyze

_IMPORTS = """
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
"""
_GOOD = """
def supported(conn, table):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table).on_conflict_do_nothing()
    elif conn.dialect.name == "postgresql":
        return postgresql_insert(table).on_conflict_do_nothing()
    raise NotImplementedError(conn.dialect.name)
"""
_UNCONDITIONAL = """
def unpaired(conn, table):
    return sqlite_insert(table).on_conflict_do_nothing()
"""
_SQLITE_ONLY_GUARDED = """
def unpaired(conn, table):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table).on_conflict_do_nothing()
    raise NotImplementedError(conn.dialect.name)
"""


def test_original_unconditional_writer_is_rejected_without_sibling_dispatch() -> None:
    assert _analyze(_IMPORTS + _UNCONDITIONAL)


def test_complete_local_dispatch_is_accepted() -> None:
    assert _analyze(_IMPORTS + _GOOD) == []


@pytest.mark.parametrize("writer", [_UNCONDITIONAL, _SQLITE_ONLY_GUARDED], ids=["unguarded", "missing-local-counterpart"])
def test_correct_sibling_cannot_authorize_another_writer(writer: str) -> None:
    source = _IMPORTS + _GOOD + writer
    tree = ast.parse(source)
    unpaired = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "unpaired")
    call = next(
        node
        for node in ast.walk(unpaired)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sqlite_insert"
    )
    findings = _analyze(source)
    assert [finding.line for finding in findings] == [call.lineno]


def _sqlite_calls(source: str, *, function: str) -> list[int]:
    tree = ast.parse(textwrap.dedent(source))
    owner = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function)
    return sorted(
        node.lineno
        for node in ast.walk(owner)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sqlite_insert"
    )


def test_another_chain_in_the_same_function_cannot_borrow_a_counterpart() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, table):
    if conn.dialect.name == "sqlite":
        sqlite_insert(table)
    elif conn.dialect.name == "postgresql":
        postgresql_insert(table)
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table)
    elif conn.dialect.name == "postgresql":
        return table
"""
    )
    assert [finding.line for finding in _analyze(source)] == _sqlite_calls(source, function="write")[1:]


def test_nested_complete_owner_cannot_authorize_incomplete_parent() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, other, table):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table)
    elif conn.dialect.name == "postgresql":
        if other.dialect.name == "sqlite":
            return sqlite_insert(table)
        elif other.dialect.name == "postgresql":
            return postgresql_insert(table)
"""
    )
    assert [finding.line for finding in _analyze(source)] == _sqlite_calls(source, function="write")[:1]


def test_different_selector_receivers_do_not_form_one_dispatch() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, other, table):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table)
    elif other.dialect.name == "postgresql":
        return postgresql_insert(table)
"""
    )
    assert [finding.line for finding in _analyze(source)] == _sqlite_calls(source, function="write")


@pytest.mark.parametrize("between", ["", 'log("selecting")\n    unrelated = table', 'if dialect == "sqlite":\n        return table'])
def test_returned_factories_keep_pairing_across_unrelated_statements(between: str) -> None:
    source = (
        _IMPORTS
        + f"""
def resolve(conn, table):
    dialect = conn.dialect.name
    if dialect == "sqlite":
        def build():
            return sqlite_insert(table)
        return build
    {between}
    if dialect == "postgresql":
        def build():
            return postgresql_insert(table)
        return build
    raise NotImplementedError(dialect)
"""
    )
    assert _analyze(source) == []


@pytest.mark.parametrize("between", ["dialect = other.dialect.name", "del dialect"])
def test_return_factory_pairing_stops_on_rebinding_or_competing_dispatch(between: str) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
def resolve(conn, other, table):
    dialect = conn.dialect.name
    if dialect == "sqlite":
        def build():
            return sqlite_insert(table)
        return build
    {between}
    if dialect == "postgresql":
        def build():
            return postgresql_insert(table)
        return build
    raise NotImplementedError(dialect)
"""
    )
    findings = _analyze(source)
    expected_line = next(number for number, line in enumerate(source.splitlines(), 1) if line.strip() == "return sqlite_insert(table)")
    assert [finding.line for finding in findings] == [expected_line]
    assert "owned by this writer" in findings[0].message


@pytest.mark.parametrize(
    "writer",
    [
        "def unpaired(sqlite_insert, table):\n    return sqlite_insert(table)\n",
        "def unpaired(sq, table):\n    return sq.insert(table)\n",
        "def unpaired(foreign, table):\n    from sqlalchemy.dialects.sqlite import insert as sqlite_insert\n    sqlite_insert = foreign\n    return sqlite_insert(table)\n",
        "def unpaired(items, table):\n    for sqlite_insert in items:\n        sqlite_insert(table)\n",
        "def unpaired(manager, table):\n    with manager as sqlite_insert:\n        sqlite_insert(table)\n",
        "def unpaired(table):\n    from other_package import insert as sqlite_insert\n    return sqlite_insert(table)\n",
    ],
)
def test_unknown_or_shadowed_calls_are_not_asserted_to_be_sqlite(writer: str) -> None:
    source = _IMPORTS + "from sqlalchemy.dialects import sqlite as sq\n" + _GOOD + writer
    assert _analyze(source) == []


def test_explicit_local_reimport_restores_known_builder_identity() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
def unpaired(sqlite_insert, table):
    sqlite_insert(table)
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    return sqlite_insert(table)
"""
    )
    assert [finding.line for finding in _analyze(source)] == _sqlite_calls(source, function="unpaired")[1:]


def test_dialect_alias_from_another_function_does_not_prove_selector() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
def initialize(conn):
    dialect = conn.dialect.name
def write(dialect, table):
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif dialect == "postgresql":
        return postgresql_insert(table)
"""
    )
    assert [finding.line for finding in _analyze(source)] == _sqlite_calls(source, function="write")


def test_unused_nested_helper_cannot_supply_the_parent_counterpart() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, table):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table)
    elif conn.dialect.name == "postgresql":
        def unused():
            return postgresql_insert(table)
        return table
"""
    )
    assert [finding.line for finding in _analyze(source)] == _sqlite_calls(source, function="write")


def test_returned_factory_uses_import_binding_at_publication() -> None:
    source = (
        _IMPORTS
        + """
def resolve(conn, table):
    if conn.dialect.name == "sqlite":
        def build():
            return sqlite_insert(table)
        from sqlalchemy.dialects.postgresql import insert as sqlite_insert
        return build
    elif conn.dialect.name == "postgresql":
        return postgresql_insert(table)
"""
    )
    findings = _analyze(source)
    assert [finding.line for finding in findings] == _sqlite_calls(source, function="resolve")
    assert "postgresql insert builder" in findings[0].message


def test_branch_join_does_not_keep_a_shadowed_module_import() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
from sqlalchemy.dialects import sqlite as sq
def unpaired(condition, foreign, table):
    if condition:
        sq = foreign
    return sq.insert(table)
"""
    )
    assert _analyze(source) == []


def test_comprehension_binding_does_not_borrow_outer_import_identity() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
def unpaired(builders, table):
    return [sqlite_insert(table) for sqlite_insert in builders]
"""
    )
    assert _analyze(source) == []


@pytest.mark.parametrize(
    "declaration",
    [
        "def unpaired(value: sqlite_insert(table)): pass",
        "def unpaired() -> sqlite_insert(table): pass",
        "class Unpaired(sqlite_insert(table)): pass",
        "unpaired = lambda value=sqlite_insert(table): value",
    ],
)
def test_eager_definition_headers_are_checked(declaration: str) -> None:
    source = _IMPORTS + _GOOD + "\n" + declaration
    tree = ast.parse(source)
    expected = max(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sqlite_insert"
    )
    assert [finding.line for finding in _analyze(source)] == [expected]


@pytest.mark.parametrize(
    "assignment,extra",
    [
        ("dialect = engine.dialect.name\n        validate(dialect)\n        self._dialect = dialect", ""),
        ("self._dialect = engine.dialect.name", ""),
    ],
)
def test_cached_dialect_selector_keeps_mysql_family_support(assignment: str, extra: str) -> None:
    source = (
        _IMPORTS
        + f"""
class Authority:
    def __init__(self, engine):
        {assignment}
    def write(self, table):
        if self._dialect == "sqlite":
            return sqlite_insert(table)
        elif self._dialect == "postgresql":
            return postgresql_insert(table)
        else:
            from sqlalchemy.dialects.mysql import insert as mysql_insert
            return mysql_insert(table)
    {extra}
"""
    )
    assert _analyze(source) == []


@pytest.mark.parametrize(
    "assignment,extra",
    [
        ("self._dialect = engine", ""),
        ("self._dialect = engine.dialect.name", "def replace(self, value):\n        self._dialect = value"),
        ("self._dialect = engine.dialect.name", "def replace(self):\n        del self._dialect"),
    ],
)
def test_unknown_or_conflicting_cached_selector_cannot_borrow_global_evidence(assignment: str, extra: str) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
class Authority:
    def __init__(self, engine):
        {assignment}
    def write(self, table):
        if self._dialect == "sqlite":
            return sqlite_insert(table)
        elif self._dialect == "postgresql":
            return postgresql_insert(table)
    {extra}
"""
    )
    assert [finding.line for finding in _analyze(source)] == _sqlite_calls(source, function="write")
