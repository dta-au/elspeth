"""Explicit parent rebinding must invalidate more-specific receiver identities."""

import pytest
from tests.unit.elspeth_lints.test_portable_sqlite_insert_rule import _analyze
from tests.unit.elspeth_lints.test_sqlite_insert_ownership import _GOOD, _IMPORTS


@pytest.mark.parametrize("rebound", [False, True])
def test_parent_rebind_after_explicit_child_write(rebound: bool) -> None:
    assignment = "conn = other" if rebound else ""
    source = (
        _IMPORTS
        + _GOOD
        + f"""
def write(conn, other, child, table):
    conn.child = child
    dialect = conn.child.dialect.name
    {assignment}
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif conn.child.dialect.name == "postgresql":
        return postgresql_insert(table)
"""
    )
    compile(source, "ordinary-child-receiver.py", "exec")
    findings = _analyze(source)
    if rebound:
        site = next(number for number, line in enumerate(source.splitlines(), 1) if line.strip() == "return sqlite_insert(table)")
        assert [finding.line for finding in findings] == [site]
    else:
        assert findings == []


@pytest.mark.parametrize(
    "setup,receiver,assignment,copied,accepted",
    [
        ("conn.child.grandchild = child", "conn.child.grandchild", "conn.child = other", False, False),
        ("conn.child = child", "conn.child", "conn = other", True, True),
        ("conn.child.grandchild = child", "conn.child.grandchild", "conn.child = other", True, True),
        ("connection.child = child", "connection.child", "conn = other", False, True),
        ("conn.childish.grandchild = child", "conn.childish.grandchild", "conn.child = other", False, True),
    ],
)
def test_parent_rebinding_preserves_captured_and_sibling_values(
    setup: str, receiver: str, assignment: str, copied: bool, accepted: bool
) -> None:
    right = "saved" if copied else f"{receiver}.dialect.name"
    source = (
        _IMPORTS
        + _GOOD
        + f"""
def write(conn, connection, other, child, table):
    {setup}
    dialect = {receiver}.dialect.name
    saved = dialect
    {assignment}
    if dialect == "sqlite":
        return sqlite_insert(table)
    elif {right} == "postgresql":
        return postgresql_insert(table)
"""
    )
    compile(source, "ordinary-parent-receiver.py", "exec")
    findings = _analyze(source)
    if accepted:
        assert findings == []
    else:
        site = next(number for number, line in enumerate(source.splitlines(), 1) if line.strip() == "return sqlite_insert(table)")
        assert [finding.line for finding in findings] == [site]


def test_parent_rebind_preserves_validated_captured_binary_domain() -> None:
    source = (
        _IMPORTS
        + _GOOD
        + """
def write(conn, other, child, table):
    conn.child = child
    dialect = conn.child.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise ValueError("unsupported")
    conn = other
    if dialect == "postgresql":
        return postgresql_insert(table)
    else:
        return sqlite_insert(table)
"""
    )
    compile(source, "ordinary-captured-domain.py", "exec")
    assert _analyze(source) == []


@pytest.mark.parametrize(
    "kind,sibling,delete", [(kind, sibling, delete) for kind in ("loop", "except") for sibling in (False, True) for delete in (False, True)]
)
def test_receiver_writes_invalidate_loop_back_edges_and_exception_entries(kind: str, sibling: bool, delete: bool) -> None:
    target = "conn.childish" if sibling else "conn.child"
    mutation = f"del {target}" if delete else f"{target} = other"
    dispatch = """if dialect == "sqlite":
    sqlite_insert(table)
elif conn.child.dialect.name == "postgresql":
    postgresql_insert(table)"""
    if kind == "loop":
        body = "for child in children:\n" + "\n".join("    " + line for line in dispatch.splitlines()) + f"\n    {mutation}"
    else:
        body = f"try:\n    {mutation}\n    risky()\nexcept Exception:\n" + "\n".join("    " + line for line in dispatch.splitlines())
    source = (
        _IMPORTS
        + _GOOD
        + "\ndef write(conn, children, other, table, risky):\n    dialect = conn.child.dialect.name\n"
        + "\n".join("    " + line for line in body.splitlines())
        + "\n"
    )
    compile(source, "ordinary-receiver-control-edges.py", "exec")
    findings = _analyze(source)
    if sibling:
        assert findings == []
    else:
        site = next(number for number, line in enumerate(source.splitlines(), 1) if line.strip() == "sqlite_insert(table)")
        assert [finding.line for finding in findings] == [site]
        assert "owned by this writer" in findings[0].message


@pytest.mark.parametrize("kind", ["loop", "except"])
@pytest.mark.parametrize("deferred", [False, True])
def test_receiver_entry_invalidation_preserves_captures_and_nested_scope_isolation(kind: str, deferred: bool) -> None:
    right = "conn.child.dialect.name" if deferred else "saved"
    dispatch = f"""if dialect == "sqlite":
    sqlite_insert(table)
elif {right} == "postgresql":
    postgresql_insert(table)"""
    mutation = "def change():\n    conn.child = other" if deferred else "conn.child = other"
    if kind == "loop":
        body = "for child in children:\n" + "\n".join("    " + line for line in (dispatch + "\n" + mutation).splitlines())
    else:
        body = "try:\n" + "\n".join("    " + line for line in mutation.splitlines()) + "\n    risky()\nexcept Exception:\n"
        body += "\n".join("    " + line for line in dispatch.splitlines())
    source = (
        _IMPORTS
        + _GOOD
        + "\ndef write(conn, children, other, table, risky):\n    dialect = saved = conn.child.dialect.name\n"
        + "\n".join("    " + line for line in body.splitlines())
        + "\n"
    )
    compile(source, "ordinary-receiver-scope-positive.py", "exec")
    assert _analyze(source) == []
