"""Ordinary controls for the two actual closed-domain corpus forms."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.unit.elspeth_lints.test_portable_sqlite_insert_rule import _analyze
from tests.unit.elspeth_lints.test_sqlite_insert_ownership import _GOOD, _IMPORTS

from elspeth_lints.rules.contract_invariants.portable_sqlite_insert.rule import find_portable_sqlite_insert_findings

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "path",
    [
        "src/elspeth/web/coordination/rate_limit_authority.py",
        "src/elspeth/web/sessions/skill_markdown_history.py",
    ],
)
def test_actual_closed_domain_dispatch_remains_accepted(path: str) -> None:
    assert find_portable_sqlite_insert_findings(ast.parse((ROOT / path).read_text()), path) == []


@pytest.mark.parametrize("domain,accepted", [('"sqlite", "postgresql"', True), ('"sqlite", "postgresql", "mysql"', False)])
def test_only_proven_binary_cached_domain_supplies_complement(domain: str, accepted: bool) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
class Authority:
    def __init__(self, engine):
        dialect = engine.dialect.name
        if dialect not in {{{domain}}}:
            raise NotImplementedError(dialect)
        self._dialect = dialect
    def write(self, table):
        if self._dialect == "sqlite":
            return sqlite_insert(table)
        else:
            return postgresql_insert(table)
"""
    )
    findings = _analyze(source)
    assert bool(findings) is not accepted


@pytest.mark.parametrize("same_engine", [True, False])
def test_validated_engine_identity_is_required_for_connection_complement(same_engine: bool) -> None:
    guard = (
        'if conn.engine is not self._engine or not conn.in_transaction():\n            raise ValueError("wrong connection")'
        if same_engine
        else "pass"
    )
    source = (
        _IMPORTS
        + _GOOD
        + f"""
class Authority:
    def __init__(self, engine):
        if engine.dialect.name not in {{"sqlite", "postgresql"}}:
            raise ValueError("unsupported")
        self._engine = engine
    def write(self, conn, table):
        {guard}
        return postgresql_insert(table) if conn.dialect.name == "postgresql" else sqlite_insert(table)
"""
    )
    assert bool(_analyze(source)) is not same_engine


def test_local_rejection_proves_binary_domain_for_conditional_expression() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, table):
    dialect = conn.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise NotImplementedError(dialect)
    return postgresql_insert(table) if dialect == "postgresql" else sqlite_insert(table)
"""
    )
    assert _analyze(source) == []


@pytest.mark.parametrize(
    "guard,after",
    [
        ('if conn.engine is not self._engine and not conn.in_transaction():\n            raise ValueError("wrong")', ""),
        ('if conn.engine is not self._other:\n            raise ValueError("wrong")', ""),
        ('if condition:\n            if conn.engine is not self._engine:\n                raise ValueError("wrong")', ""),
        ('if conn.engine is not self._engine:\n            raise ValueError("wrong")', "conn = other"),
    ],
)
def test_connection_domain_proof_is_exact_and_dominates_dispatch(guard: str, after: str) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
class Authority:
    def __init__(self, engine):
        if engine.dialect.name not in {{"sqlite", "postgresql"}}:
            raise ValueError("unsupported")
        self._engine = engine
    def write(self, conn, other, condition, table):
        {guard}
        {after}
        return postgresql_insert(table) if conn.dialect.name == "postgresql" else sqlite_insert(table)
"""
    )
    findings = _analyze(source)
    assert len(findings) == 1
    assert "owned by this writer" in findings[0].message


@pytest.mark.parametrize(
    "validation,assignment",
    [
        ('if dialect not in {"sqlite", "postgresql"}:\n            pass', "self._dialect = dialect"),
        (
            'if dialect not in {"sqlite", "postgresql"}:\n            raise ValueError("unsupported")',
            'dialect = "mysql"\n        self._dialect = dialect',
        ),
    ],
)
def test_cached_domain_requires_rejection_and_same_captured_value(validation: str, assignment: str) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
class Authority:
    def __init__(self, engine):
        dialect = engine.dialect.name
        {validation}
        {assignment}
    def write(self, table):
        if self._dialect == "sqlite":
            return sqlite_insert(table)
        else:
            return postgresql_insert(table)
"""
    )
    assert len(_analyze(source)) == 1


def test_swapped_builder_in_proven_complement_keeps_primary_diagnostic() -> None:
    source = (
        _IMPORTS
        + """
class Authority:
    def __init__(self, engine):
        dialect = engine.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise ValueError("unsupported")
        self._dialect = dialect
    def write(self, table):
        if self._dialect == "sqlite":
            return sqlite_insert(table)
        else:
            return sqlite_insert(table)
"""
    )
    findings = _analyze(source)
    assert len(findings) == 1
    assert "Dialect branch for 'postgresql'" in findings[0].message


def test_complete_conditional_expression_does_not_authorize_another_writer() -> None:
    source = (
        _IMPORTS
        + """
def write(conn, table):
    dialect = conn.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise NotImplementedError(dialect)
    supported = postgresql_insert(table) if dialect == "postgresql" else sqlite_insert(table)
    return sqlite_insert(table)
"""
    )
    findings = _analyze(source)
    assert len(findings) == 1
    assert "owned by this writer" in findings[0].message
