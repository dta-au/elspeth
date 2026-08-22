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
from pathlib import Path

from elspeth.contracts.identity import TokenInfo
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.core.landscape import schema

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "elspeth"

RETIRED_FIELD_NAMES = {"branch_name", "fork_group_id", "expand_group_id"}

# Columns with retired names that deliberately SURVIVE, with their reasons:
COLUMN_ALLOWLIST = {
    ("tokens", "join_group_id"),  # merge-event anchor; coalesce_effects composite FK target (spec §4.1)
    ("token_work_items", "join_group_id"),  # merged-token work-item carrier (ruling 20, plan decision D1)
    ("coalesce_branch_losses", "branch_name"),  # WS3 replaces this table with group_losses; until then it stands
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
    - Functional form: insert(token_lineage_frames_table)
    - Simple aliases: _frames = token_lineage_frames_table; _frames.insert()

    Out of scope: raw SQL via text() or connectable.execute(text(...)) —
    those are not AST-detectable without a type-inference pass.
    """
    hits: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        # First pass: collect straight-line aliases (Name → table_attr) at module scope
        # and within functions. Only one hop: _frames = token_lineage_frames_table.
        aliases_to_table: set[str] = set()
        for node in ast.walk(tree):
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
            is_match = False

            # Attribute form: <table_or_alias>.insert()
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "insert"
                and isinstance(node.func.value, (ast.Name, ast.Attribute))
            ):
                receiver = node.func.value
                receiver_name = receiver.id if isinstance(receiver, ast.Name) else receiver.attr
                if receiver_name == table_attr or receiver_name in aliases_to_table:
                    is_match = True

            # Functional form: insert(token_lineage_frames_table)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "insert":
                for arg in node.args:
                    if isinstance(arg, ast.Name) and (arg.id == table_attr or arg.id in aliases_to_table):
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
