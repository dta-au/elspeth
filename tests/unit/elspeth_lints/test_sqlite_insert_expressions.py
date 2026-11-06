"""Current-source controls for enclosing-scope assignment expressions."""

from __future__ import annotations

import pytest
from tests.unit.elspeth_lints.test_portable_sqlite_insert_rule import _analyze
from tests.unit.elspeth_lints.test_sqlite_insert_ownership import _GOOD, _IMPORTS


def _assert_bad_site(source: str) -> None:
    compile(source, "ordinary-sqlite-regression.py", "exec")
    sites = [n for n, line in enumerate(source.splitlines(), 1) if line.strip() == "return sqlite_insert(table)"]
    assert len(sites) == 1
    findings = _analyze(source)
    assert [finding.line for finding in findings] == sites
    assert "owned by this writer" in findings[0].message


@pytest.mark.parametrize(
    "expression",
    [
        '[(dialect := "sqlite") for _ in (0,)]',
        '{(dialect := "sqlite") for _ in (0,)}',
        '{0: (dialect := "sqlite") for _ in (0,)}',
        "[(dialect := other.dialect.name) for _ in (0,)]",
        'list((dialect := "sqlite") for _ in (0,))',
        '[(dialect := conn.dialect.name) for _ in (0,) if (dialect := "sqlite") == "postgresql"]',
    ],
)
def test_eager_comprehension_assignment_cannot_retain_old_selector(expression: str) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
def write(conn, other, table):
    dialect = conn.dialect.name
    {expression}
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif dialect == "postgresql":
        return postgresql_insert(table)
"""
    )
    _assert_bad_site(source)


def test_comprehension_receiver_rebind_cannot_pair_with_immutable_old_selector() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
def write(conn, other, table):
    dialect = conn.dialect.name
    [(conn := other) for _ in (0,)]
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif conn.dialect.name == "postgresql":
        return postgresql_insert(table)
"""
    )
    _assert_bad_site(source)


@pytest.mark.parametrize(
    "expression",
    [
        "[(dialect := conn.dialect.name) for _ in (0,)]",
        "[(dialect := conn.dialect.name) for _ in ()]",
        'deferred = ((dialect := "sqlite") for _ in (0,))',
        "[conn for conn in (other,)]",
        '[lambda: (dialect := "sqlite") for _ in (0,)]',
        "[(dialect := conn.dialect.name) for _ in (0,) if (dialect := conn.dialect.name)]",
        '{(dialect := "sqlite"): (dialect := conn.dialect.name) for _ in (0,)}',
    ],
)
def test_comprehension_scope_and_same_origin_captures_remain_supported(expression: str) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
def write(conn, other, table):
    dialect = conn.dialect.name
    {expression}
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif dialect == "postgresql":
        return postgresql_insert(table)
"""
    )
    compile(source, "ordinary-sqlite-regression.py", "exec")
    assert _analyze(source) == []


@pytest.mark.parametrize("populated", [False, True])
def test_python_keyword_then_generator_consumption_control(populated: bool) -> None:
    dialect = "postgresql"

    def reset() -> None:
        nonlocal dialect
        dialect = "postgresql"

    sorted(
        ((dialect := "sqlite") for _ in ((0,) if populated else ())),
        key=reset(),
    )
    assert dialect == ("sqlite" if populated else "postgresql")


@pytest.mark.parametrize("body,accepted", [('"sqlite"', False), ("conn.dialect.name", True)])
def test_keyword_recapture_cannot_overwrite_later_generator_effect(body: str, accepted: bool) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
def write(conn, table):
    dialect = conn.dialect.name
    sorted(((dialect := {body}) for _ in (0,)), key=(None if (dialect := conn.dialect.name) else None))
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif dialect == "postgresql":
        return postgresql_insert(table)
"""
    )
    if accepted:
        compile(source, "ordinary-consumer-order.py", "exec")
        assert _analyze(source) == []
    else:
        _assert_bad_site(source)


@pytest.mark.parametrize("shadowed", [False, True])
def test_generator_builder_identity_uses_bindings_after_keywords(shadowed: bool) -> None:
    keyword = ", key=(None if (builder := unrelated) else None)" if shadowed else ""
    source = (
        _IMPORTS
        + _GOOD
        + f"""
def write(conn, table, unrelated):
    from sqlalchemy.dialects.sqlite import insert as builder
    if conn.dialect.name == "sqlite":
        sorted((builder(table) for _ in (0,)){keyword})
"""
    )
    compile(source, "ordinary-consumer-builder.py", "exec")
    findings = _analyze(source)
    if shadowed:
        assert findings == []
    else:
        site = next(number for number, line in enumerate(source.splitlines(), 1) if "sorted((builder(table)" in line)
        assert [finding.line for finding in findings] == [site]


@pytest.mark.parametrize(
    "expression",
    [
        '(dialect := "sqlite")',
        '(dialect := "mysql")',
        "(dialect := other.dialect.name)",
        '[(dialect := "sqlite") for _ in (0,)]',
        'lambda unused=(dialect := "sqlite"): None',
    ],
)
def test_constructor_value_effects_cannot_retain_validated_cache(expression: str) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
class Authority:
    def __init__(self, engine, other):
        dialect = engine.dialect.name
        if dialect not in {{"sqlite", "postgresql"}}:
            raise ValueError("unsupported")
        label = {expression}
        self._dialect = dialect
    def write(self, table):
        if self._dialect == "postgresql":
            return postgresql_insert(table)
        else:
            return sqlite_insert(table)
"""
    )
    _assert_bad_site(source)


@pytest.mark.parametrize("expression", ["(dialect := engine.dialect.name)", 'lambda: (dialect := "sqlite")'])
def test_constructor_same_origin_or_deferred_local_assignment_keeps_cache(expression: str) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
class Authority:
    def __init__(self, engine):
        dialect = engine.dialect.name
        if dialect not in {{"sqlite", "postgresql"}}:
            raise ValueError("unsupported")
        label = {expression}
        self._dialect = dialect
    def write(self, table):
        if self._dialect == "postgresql":
            return postgresql_insert(table)
        else:
            return sqlite_insert(table)
"""
    )
    compile(source, "ordinary-sqlite-regression.py", "exec")
    assert _analyze(source) == []


def test_proven_complement_reaches_ordinary_nested_condition() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
def write(conn, table, enabled):
    dialect = conn.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise ValueError("unsupported")
    if dialect == "postgresql":
        return postgresql_insert(table)
    else:
        if enabled:
            return sqlite_insert(table)
"""
    )
    assert _analyze(source) == []


def test_nested_else_condition_still_requires_a_proven_binary_domain() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
def write(conn, table, enabled):
    dialect = conn.dialect.name
    if dialect == "postgresql":
        return postgresql_insert(table)
    else:
        if enabled:
            return sqlite_insert(table)
"""
    )
    _assert_bad_site(source)


def test_swapped_builder_in_nested_complement_keeps_wrong_branch_diagnostic() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
def write(conn, table, enabled):
    dialect = conn.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise ValueError("unsupported")
    if dialect == "postgresql":
        return postgresql_insert(table)
    else:
        if enabled:
            return postgresql_insert(table)
"""
    )
    compile(source, "ordinary-sqlite-regression.py", "exec")
    findings = _analyze(source)
    expected_line = max(number for number, line in enumerate(source.splitlines(), 1) if line.strip() == "return postgresql_insert(table)")
    assert [finding.line for finding in findings] == [expected_line]
    assert "Dialect branch for 'sqlite' invokes the postgresql insert builder" in findings[0].message
