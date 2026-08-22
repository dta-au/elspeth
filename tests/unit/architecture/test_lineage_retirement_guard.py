"""tests/unit/architecture/test_lineage_retirement_guard.py

Whole-tree gate for the unified-lineage retirement (spec §11). A green scoped
run elsewhere proves nothing about this file's subject — it asserts over the
ENTIRE src tree.
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
    hits: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # matches <table_attr>.insert() — attribute chain ending in the table name then .insert
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "insert"
                and isinstance(node.func.value, (ast.Name, ast.Attribute))
                and (getattr(node.func.value, "id", None) == table_attr or getattr(node.func.value, "attr", None) == table_attr)
            ):
                hits.add(str(path.relative_to(SRC_ROOT)))
    return hits


def test_token_lineage_frames_has_one_write_path() -> None:
    writers = _modules_inserting_into("token_lineage_frames_table")
    assert writers == {"core/landscape/data_flow/tokens.py"}, (
        f"token_lineage_frames must have exactly one writer module (spec §11 sole-write-path rule); found {sorted(writers)}"
    )
