"""Tests for SQLite builder scopes and expression provenance."""

from __future__ import annotations

import pytest
from tests.unit.elspeth_lints.test_portable_sqlite_insert_rule import _analyze
from tests.unit.elspeth_lints.test_sqlite_insert_ownership import _GOOD, _IMPORTS, _sqlite_calls


@pytest.mark.parametrize(
    "imports,builder",
    [
        (_IMPORTS, "sqlite_insert"),
        (
            "from sqlalchemy.dialects import sqlite as sq\nfrom sqlalchemy.dialects.postgresql import insert as postgresql_insert\n",
            "sq.insert",
        ),
    ],
)
def test_deferred_global_imports_still_identify_wrong_builder(imports: str, builder: str) -> None:
    source = (
        f"""def bad(conn, table):
    if conn.dialect.name == "postgresql":
        return {builder}(table)
"""
        + imports
        + _GOOD.replace("sqlite_insert", builder)
    )
    findings = _analyze(source)
    assert [finding.line for finding in findings] == [3]
    assert "postgresql" in findings[0].message


def test_rebinding_receiver_distinguishes_copied_selector_snapshots() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, other, table):
    original = conn.dialect.name
    conn = other
    replacement = conn.dialect.name
    if original == "sqlite":
        return sqlite_insert(table)
    elif replacement == "postgresql":
        return postgresql_insert(table)
"""
    )
    assert [finding.line for finding in _analyze(source)] == _sqlite_calls(source, function="write")


def test_copies_before_receiver_rebinding_keep_same_selector_snapshot() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, other, table):
    original = conn.dialect.name
    same = original
    conn = other
    if original == "sqlite":
        return sqlite_insert(table)
    elif same == "postgresql":
        return postgresql_insert(table)
"""
    )
    assert _analyze(source) == []


def test_eager_class_body_keeps_selecting_owner() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, table):
    if conn.dialect.name == "sqlite":
        class Selected:
            statement = sqlite_insert(table)
        return Selected.statement
    elif conn.dialect.name == "postgresql":
        class Selected:
            statement = postgresql_insert(table)
        return Selected.statement
"""
    )
    assert _analyze(source) == []


def test_unused_class_method_does_not_supply_parent_counterpart() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, table):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table)
    elif conn.dialect.name == "postgresql":
        class Selected:
            def unused(self):
                return postgresql_insert(table)
        return table
"""
    )
    assert [finding.line for finding in _analyze(source)] == _sqlite_calls(source, function="write")


@pytest.mark.parametrize(
    "expression,accepted",
    [
        ("(postgresql_insert(table) for item in (table,))", False),
        ("(item for item in (postgresql_insert(table),))", True),
        ("[postgresql_insert(table) for item in (table,)]", True),
        ("list(postgresql_insert(table) for item in (table,))", True),
    ],
)
def test_generator_creation_and_eager_counterpart_shapes(expression: str, accepted: bool) -> None:
    source = (
        _IMPORTS
        + f"""
def write(conn, table):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table)
    elif conn.dialect.name == "postgresql":
        unused = {expression}
        return table
"""
    )
    findings = _analyze(source)
    assert [finding.line for finding in findings] == ([] if accepted else _sqlite_calls(source, function="write"))


def test_redundant_excluded_guard_with_builder_is_still_unreachable() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, table):
    dialect = conn.dialect.name
    if dialect == "sqlite":
        return sqlite_insert(table)
    if dialect == "sqlite":
        return sqlite_insert(table)
    if dialect == "postgresql":
        return postgresql_insert(table)
"""
    )
    assert _analyze(source) == []


def test_unconditional_named_expression_keeps_local_selector_provenance() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, table):
    (dialect := conn.dialect.name)
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif dialect == "postgresql":
        return postgresql_insert(table)
"""
    )
    assert _analyze(source) == []


def test_default_expression_rebinding_precedes_annotation_evaluation() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
def unpaired(value: sqlite_insert(table) = (sqlite_insert := foreign)):
    pass
"""
    )
    assert _analyze(source) == []


@pytest.mark.parametrize("receiver", ["self", "instance"])
def test_cached_selector_supports_positional_only_instance_receiver(receiver: str) -> None:
    source = (
        _IMPORTS
        + f"""
class Authority:
    def __init__({receiver}, /, engine):
        dialect = engine.dialect.name
        {receiver}._dialect = dialect
    def write({receiver}, /, table):
        if {receiver}._dialect == "sqlite":
            return sqlite_insert(table)
        elif {receiver}._dialect == "postgresql":
            return postgresql_insert(table)
"""
    )
    assert _analyze(source) == []


def test_excluded_guard_does_not_launder_its_imports_into_later_arm() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, table):
    global postgresql_insert
    dialect = conn.dialect.name
    if dialect == "sqlite":
        return sqlite_insert(table)
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as postgresql_insert
    if dialect == "postgresql":
        return postgresql_insert(table)
"""
    )
    assert _analyze(source) == []


@pytest.mark.parametrize(
    "setup,consumer,accepted",
    [
        ("", "list", True),
        ("from builtins import list as consume", "consume", True),
        ("import builtins", "builtins.list", True),
        ("def list(value): return value", "list", False),
    ],
)
def test_inline_generator_consumption_requires_positive_builtin_identity(setup: str, consumer: str, accepted: bool) -> None:
    source = (
        _IMPORTS
        + "\n"
        + setup
        + f"""
def write(conn, table):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table)
    elif conn.dialect.name == "postgresql":
        return {consumer}(postgresql_insert(table) for item in (table,))
"""
    )
    assert bool(_analyze(source)) is not accepted


def test_unrelated_conditional_expression_preserves_enclosing_owner() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, flag, first, second):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(first) if flag else sqlite_insert(second)
    elif conn.dialect.name == "postgresql":
        return postgresql_insert(first) if flag else postgresql_insert(second)
"""
    )
    assert _analyze(source) == []


def test_unrelated_conditional_expression_keeps_wrong_builder_diagnostic() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, flag, first, second):
    if conn.dialect.name == "sqlite":
        return postgresql_insert(first) if flag else sqlite_insert(second)
    elif conn.dialect.name == "postgresql":
        return postgresql_insert(first) if flag else postgresql_insert(second)
"""
    )
    findings = _analyze(source)
    assert len(findings) == 1
    assert "Dialect branch for 'sqlite'" in findings[0].message


@pytest.mark.parametrize("operator", ["and", "or"])
def test_optional_named_expression_does_not_prove_parameter_selector(operator: str) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
def write(conn, enabled, dialect, table):
    enabled {operator} (dialect := conn.dialect.name)
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif dialect == "postgresql":
        return postgresql_insert(table)
"""
    )
    assert [finding.line for finding in _analyze(source)] == _sqlite_calls(source, function="write")


@pytest.mark.parametrize("operator", ["and", "or"])
def test_optional_capture_of_same_proven_origin_remains_accepted(operator: str) -> None:
    source = (
        _IMPORTS
        + f"""
def write(conn, enabled, table):
    dialect = conn.dialect.name
    enabled {operator} (dialect := conn.dialect.name)
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif dialect == "postgresql":
        return postgresql_insert(table)
"""
    )
    assert _analyze(source) == []


def test_conditional_expression_cannot_keep_proof_after_optional_foreign_capture() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
def write(conn, other, enabled, table):
    dialect = conn.dialect.name
    (dialect := other.dialect.name) if enabled else None
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif dialect == "postgresql":
        return postgresql_insert(table)
"""
    )
    assert [finding.line for finding in _analyze(source)] == _sqlite_calls(source, function="write")


@pytest.mark.parametrize(
    "setup,decorator",
    [
        ("", "staticmethod"),
        ("import builtins", "builtins.staticmethod"),
        ("from builtins import staticmethod as static", "static"),
        ("from builtins import classmethod as class_method", "class_method"),
    ],
)
def test_static_method_cannot_borrow_instance_cached_binary_domain(setup: str, decorator: str) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + "\n"
        + setup
        + f"""
class Authority:
    def __init__(self, engine):
        dialect = engine.dialect.name
        if dialect not in {{"sqlite", "postgresql"}}:
            raise ValueError("unsupported")
        self._dialect = dialect
    @{decorator}
    def write(other, table):
        if other._dialect == "sqlite":
            return sqlite_insert(table)
        else:
            return postgresql_insert(table)
"""
    )
    findings = _analyze(source)
    assert len(findings) == 1
    assert "owned by this writer" in findings[0].message


def test_descriptor_identity_is_captured_before_default_rebinding() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
from builtins import staticmethod as static
class Authority:
    def __init__(self, engine):
        dialect = engine.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise ValueError("unsupported")
        self._dialect = dialect
    @static
    def write(other, table=(static := lambda value: value)):
        if other._dialect == "sqlite":
            return sqlite_insert(table)
        else:
            return postgresql_insert(table)
"""
    )
    findings = _analyze(source)
    assert len(findings) == 1
    assert "owned by this writer" in findings[0].message
