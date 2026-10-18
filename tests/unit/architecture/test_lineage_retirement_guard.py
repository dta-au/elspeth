"""tests/unit/architecture/test_lineage_retirement_guard.py

Whole-tree gate for the unified-lineage retirement (spec §11). A green scoped
run elsewhere proves nothing about this file's subject — it asserts over the
ENTIRE src tree.

The write-path scan's alias collection is module-wide, not scope-aware: a local
name bound to token_lineage_frames_table in one function is treated as an alias
throughout that module, so an unrelated same-named local writing a different
table can trip the guard. That error is fail-closed (a spurious failure, never
a masked writer) — if it fires on a name collision, rename the local rather
than weakening the scan.
"""

from __future__ import annotations

import ast
import dataclasses
import sys
from pathlib import Path

import pytest
from tests.helpers.tree_gate import iter_gate_sources

from elspeth.contracts.identity import TokenInfo
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.core.landscape import schema

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "elspeth"

RETIRED_FIELD_NAMES = {"branch_name", "fork_group_id", "expand_group_id"}

# Columns with retired names that deliberately SURVIVE, with their reasons:
COLUMN_ALLOWLIST = {
    ("tokens", "join_group_id"),  # merge-event anchor; coalesce_effects composite FK target (spec §4.1)
    ("token_work_items", "join_group_id"),  # merged-token work-item carrier (ruling 20, plan decision D1)
}


def test_no_stored_retired_lineage_fields_on_token_contracts() -> None:
    for cls in (TokenInfo, TokenWorkItem):
        stored = {f.name for f in dataclasses.fields(cls)}
        illegal = stored & RETIRED_FIELD_NAMES
        assert not illegal, f"{cls.__name__} regrew stored lineage fields {sorted(illegal)} — ruling 21 forbids it"
    assert "join_group_id" not in {f.name for f in dataclasses.fields(TokenInfo)}, "join_group_id left TokenInfo (ruling 20)"
    assert "lineage_path" in {f.name for f in dataclasses.fields(TokenInfo)}


def test_no_retired_name_columns_outside_the_allowlist() -> None:
    violations: list[str] = []
    for table in schema.metadata.tables.values():
        for column in table.columns:
            if column.name in (RETIRED_FIELD_NAMES | {"join_group_id"}) and (table.name, column.name) not in COLUMN_ALLOWLIST:
                violations.append(f"{table.name}.{column.name}")
    assert not violations, (
        f"retired lineage column names reappeared outside the allowlist: {violations} "
        "(spec §11 — extend COLUMN_ALLOWLIST only with an adjudicated reason)"
    )


def test_expected_branches_json_stays_deleted() -> None:
    assert "expected_branches_json" not in schema.token_outcomes_table.c, "decision D2 — roster derives from child frames"


def _modules_inserting_into(table_attr: str) -> set[str]:
    """Scan for writes to a table by name, catching both attribute and functional forms.

    Detects:
    - Attribute form: token_lineage_frames_table.insert()
    - Functional forms: insert(table), sqlalchemy.insert(table), and imported
      insert aliases from SQLAlchemy or its PostgreSQL/SQLite dialects.
      Table operands may be bare names or qualified attributes.
    - Simple aliases: _frames = token_lineage_frames_table; _frames.insert()

    Bindings are module-wide, like the existing table aliases. Bare insert
    remains a conservative fallback even without an import. Raw SQL, dynamic
    callable aliases and keyword table operands remain outside this syntax gate.
    """
    hits: set[str] = set()
    for parsed in iter_gate_sources(SRC_ROOT):
        path = parsed.path
        tree = parsed.tree

        # First pass: collect module-wide import bindings and one-hop table aliases.
        # Table aliases may be declared at module scope or inside functions.
        aliases_to_table: set[str] = set()
        insert_names = {"insert"}
        sqlalchemy_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                sqlalchemy_modules.update(alias.asname or alias.name for alias in node.names if alias.name == "sqlalchemy")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module in {"sqlalchemy", "sqlalchemy.dialects.postgresql", "sqlalchemy.dialects.sqlite"}
            ):
                insert_names.update(alias.asname or alias.name for alias in node.names if alias.name == "insert")
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Name)
                and node.value.id == table_attr
            ):
                target_name = node.targets[0].id
                aliases_to_table.add(target_name)

        # Second pass: find insert() calls on the table or its aliases
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_match = False

            # Attribute form: <table_or_alias>.insert()
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "insert"
                and isinstance(node.func.value, (ast.Name, ast.Attribute))
            ):
                receiver = node.func.value
                receiver_name = receiver.id if isinstance(receiver, ast.Name) else receiver.attr
                if receiver_name == table_attr or receiver_name in aliases_to_table:
                    is_match = True

            # Resolve supported functional forms independently of table methods:
            # sa.insert(table) also has an Attribute callable.
            functional_insert = (isinstance(node.func, ast.Name) and node.func.id in insert_names) or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "insert"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in sqlalchemy_modules
            )
            if functional_insert:
                for arg in node.args:
                    if isinstance(arg, (ast.Name, ast.Attribute)):
                        operand_name = arg.id if isinstance(arg, ast.Name) else arg.attr
                        if operand_name == table_attr or operand_name in aliases_to_table:
                            is_match = True
                            break

            if is_match:
                hits.add(str(path.relative_to(SRC_ROOT)))
                break  # One hit per module is enough

    return hits


def test_token_lineage_frames_has_one_write_path() -> None:
    writers = _modules_inserting_into("token_lineage_frames_table")
    assert writers == {"core/landscape/data_flow/tokens.py"}, (
        f"token_lineage_frames must have exactly one writer module (spec §11 sole-write-path rule); found {sorted(writers)}"
    )


@pytest.mark.parametrize(
    ("source", "is_writer"),
    [
        pytest.param("token_lineage_frames_table.insert()", True, id="table-method"),
        pytest.param("models.token_lineage_frames_table.insert()", True, id="qualified-table-method"),
        pytest.param(
            "frames = token_lineage_frames_table\nframes.insert()",
            True,
            id="existing-table-alias",
        ),
        pytest.param(
            "from sqlalchemy import insert\ninsert(token_lineage_frames_table)",
            True,
            id="existing-functional-insert",
        ),
        pytest.param(
            "import sqlalchemy\nsqlalchemy.insert(token_lineage_frames_table)",
            True,
            id="sqlalchemy-module",
        ),
        pytest.param(
            "import sqlalchemy as sa\nsa.insert(token_lineage_frames_table)",
            True,
            id="sqlalchemy-module-alias",
        ),
        pytest.param(
            "from sqlalchemy import insert as emit\nemit(token_lineage_frames_table)",
            True,
            id="sqlalchemy-insert-alias",
        ),
        pytest.param(
            "from sqlalchemy.dialects.postgresql import insert as postgresql_insert\npostgresql_insert(token_lineage_frames_table)",
            True,
            id="postgresql-insert-alias",
        ),
        pytest.param(
            "from sqlalchemy.dialects.sqlite import insert as sqlite_insert\nsqlite_insert(token_lineage_frames_table)",
            True,
            id="sqlite-insert-alias",
        ),
        pytest.param(
            "from sqlalchemy import insert\ninsert(models.token_lineage_frames_table)",
            True,
            id="qualified-table-operand",
        ),
        pytest.param(
            "from sqlalchemy import insert as emit\nemit(unrelated_table)",
            False,
            id="other-table-functional-insert",
        ),
        pytest.param("unrelated_table.insert()", False, id="other-table-method"),
        pytest.param(
            "from unrelated_library import insert as emit\nemit(token_lineage_frames_table)",
            False,
            id="unrelated-import-alias",
        ),
        pytest.param("helper.insert(token_lineage_frames_table)", False, id="unrelated-receiver"),
    ],
)
def test_write_path_scan_recognizes_insert_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    is_writer: bool,
) -> None:
    # Parse real temporary modules through the production gate walker. The
    # specimen's imports and insert calls are source text, never executed.
    (tmp_path / "writer.py").write_text(source + "\n")
    (tmp_path / "read_only.py").write_text("token_lineage_frames_table.select()\n")
    monkeypatch.setattr(sys.modules[__name__], "SRC_ROOT", tmp_path)

    observed = _modules_inserting_into("token_lineage_frames_table")

    assert observed == ({"writer.py"} if is_writer else set())
