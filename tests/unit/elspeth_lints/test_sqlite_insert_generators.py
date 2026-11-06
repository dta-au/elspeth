"""Possible inline consumption can invalidate facts without owning dispatch."""

from collections.abc import Iterator

import pytest
from tests.unit.elspeth_lints.test_portable_sqlite_insert_rule import _analyze
from tests.unit.elspeth_lints.test_sqlite_insert_ownership import _GOOD, _IMPORTS


@pytest.mark.parametrize("leading_positional", [False, True])
def test_python_starred_generator_consumption_order(leading_positional: bool) -> None:
    events: list[str] = []
    dialect = "postgresql"

    def values() -> Iterator[int]:
        nonlocal dialect
        events.append("body")
        dialect = "sqlite"
        yield 0

    def keyword() -> None:
        nonlocal dialect
        events.append("keyword")
        dialect = "postgresql"

    def consume(*args: int, key: None = None) -> None:
        assert key is None
        assert len(args) == 1 + int(leading_positional)
        events.append("consumer")

    if leading_positional:
        consume(0, *values(), key=keyword())
    else:
        consume(*values(), key=keyword())
    assert events == (["body", "keyword", "consumer"] if leading_positional else ["keyword", "body", "consumer"])
    assert dialect == ("postgresql" if leading_positional else "sqlite")


def _bad(source: str) -> None:
    compile(source, "ordinary-generator-effect.py", "exec")
    site = next(number for number, line in enumerate(source.splitlines(), 1) if line.strip() == "return sqlite_insert(table)")
    findings = _analyze(source)
    assert [finding.line for finding in findings] == [site]
    assert "owned by this writer" in findings[0].message


@pytest.mark.parametrize(
    "expression",
    [
        'any((dialect := "sqlite") for _ in (0,))',
        'all((dialect := "sqlite") for _ in (0,))',
        'next((dialect := "sqlite") for _ in (0,))',
        'list((dialect := "sqlite") for _ in (0,))',
        'consume(values=((dialect := "sqlite") for _ in (0,)))',
        'consume(*((dialect := "sqlite") for _ in (0,)))',
        "any((dialect := other.dialect.name) for _ in (0,))",
        'consume(((dialect := "sqlite") for _ in (0,)), key=(None if (dialect := conn.dialect.name) else None))',
        "consume(((dialect := saved) for _ in (0,)), ((saved := other.dialect.name) for _ in (0,)))",
        'consume(*((dialect := "sqlite") for _ in (0,)), key=(None if (dialect := conn.dialect.name) else None))',
        'consume(*((dialect := "sqlite") for _ in (0,)), **{"key": (dialect := conn.dialect.name)})',
        "consume(*((dialect := saved) for _ in (0,)), key=(saved := other.dialect.name))",
    ],
)
def test_possible_consumer_cannot_retain_stale_selector(expression: str) -> None:
    _bad(
        _IMPORTS
        + _GOOD
        + f"""
def write(conn, other, table, consume, list):
    dialect = saved = conn.dialect.name
    {expression}
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif dialect == "postgresql":
        return postgresql_insert(table)
"""
    )


@pytest.mark.parametrize(
    "expression",
    [
        "any((dialect := conn.dialect.name) for _ in (0,))",
        'pending = ((dialect := "sqlite") for _ in (0,))',
        'consume(0, *((dialect := "sqlite") for _ in (0,)), key=(None if (dialect := conn.dialect.name) else None))',
        'consume(*((dialect := "sqlite") for _ in (0,)), 0, key=(None if (dialect := conn.dialect.name) else None))',
    ],
)
def test_stable_capture_and_construction_order_remain_supported(expression: str) -> None:
    source = (
        _IMPORTS
        + _GOOD
        + f"""
def write(conn, table, consume):
    dialect = conn.dialect.name
    {expression}
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif dialect == "postgresql":
        return postgresql_insert(table)
"""
    )
    compile(source, "ordinary-generator-positive.py", "exec")
    assert _analyze(source) == []


def test_possible_generator_consumption_cannot_retain_constructor_cache() -> None:
    _bad(
        _IMPORTS
        + _GOOD
        + """
class Authority:
    def __init__(self, engine):
        dialect = engine.dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise ValueError("unsupported")
        label = any((dialect := "sqlite") for _ in (0,))
        self._dialect = dialect
    def write(self, table):
        if self._dialect == "postgresql":
            return postgresql_insert(table)
        else:
            return sqlite_insert(table)
"""
    )


def test_unknown_consumer_cannot_supply_dispatch_ownership() -> None:
    _bad(
        _IMPORTS
        + _GOOD
        + """
def write(conn, table, consume):
    if conn.dialect.name == "sqlite":
        return sqlite_insert(table)
    elif conn.dialect.name == "postgresql":
        consume((postgresql_insert(table) for _ in (0,)))
"""
    )
