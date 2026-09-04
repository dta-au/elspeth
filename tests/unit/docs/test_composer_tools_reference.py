"""Contracts for the user-facing Composer mutation-tool reference."""

from pathlib import Path
from typing import Any

from elspeth.web.composer.tools._dispatch import get_tool_definitions

REPO_ROOT = Path(__file__).resolve().parents[3]
REFERENCE = REPO_ROOT / "docs/reference/composer-tools.md"


def _reference() -> str:
    return REFERENCE.read_text(encoding="utf-8")


def _table_row(section: str, first_cell: str) -> str:
    marker = f"| `{first_cell}` |"
    return next(line for line in section.splitlines() if line.startswith(marker))


def _list_item(section: str, code_name: str) -> str:
    marker = f"- A `{code_name}`"
    lines = section.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(marker))
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("- ")), len(lines))
    return " ".join(line.strip() for line in lines[start:end])


def _tool_schema(name: str) -> dict[str, Any]:
    return next(definition["parameters"] for definition in get_tool_definitions() if definition["name"] == name)


def test_upsert_node_reference_tracks_structural_node_schema() -> None:
    reference = _reference()
    upsert = reference.split("### `upsert_node`", maxsplit=1)[1].split("### `upsert_edge`", maxsplit=1)[0]
    node_types = _tool_schema("upsert_node")["properties"]["node_type"]["enum"]

    for node_type in node_types:
        assert f"`{node_type}`" in upsert

    for structural_field in ("branches", "timeout_seconds"):
        assert f"| `{structural_field}` |" in upsert


def test_upsert_node_reference_states_row_union_authoring_contract() -> None:
    reference = _reference()
    upsert = reference.split("### `upsert_node`", maxsplit=1)[1].split("### `upsert_edge`", maxsplit=1)[0]
    row_union_details = upsert.split("For `row_union`", maxsplit=1)[1].split("**Structural fan-in choices.**", maxsplit=1)[0]
    row_union_row = _table_row(upsert, "row_union")

    assert "| `row_union` | `branches`, `on_success` |" in row_union_row
    for contract_term in ("plugin-free", "`require_all`", "N-to-N", "declared branch order"):
        assert contract_term in row_union_row
    for routing_field in ("`input`", "`on_success`"):
        assert routing_field in row_union_details
    for excluded_field in ("`policy`", "`merge`", "`options`"):
        assert excluded_field in row_union_details


def test_reference_distinguishes_queue_row_union_and_coalesce() -> None:
    reference = _reference()

    fan_in = reference.split("**Structural fan-in choices.**", maxsplit=1)[1].split("---", maxsplit=1)[0]
    queue = _list_item(fan_in, "queue")
    row_union = _list_item(fan_in, "row_union")
    coalesce = _list_item(fan_in, "coalesce")

    assert "pass-through" in queue and "coordination" in queue
    assert "N-to-N" in row_union
    assert "N-to-1" in coalesce


def test_set_pipeline_reference_includes_current_node_inventory() -> None:
    reference = _reference()
    set_pipeline = reference.split("### `set_pipeline`", maxsplit=1)[1].split("### `clear_source`", maxsplit=1)[0]

    for node_type in _tool_schema("upsert_node")["properties"]["node_type"]["enum"]:
        assert f"`{node_type}`" in set_pipeline
    assert "timeout_seconds?" in set_pipeline
