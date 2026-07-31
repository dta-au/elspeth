"""Contracts for the user-facing Composer mutation-tool reference."""

from pathlib import Path
from typing import Any

from elspeth.web.composer.tools._dispatch import get_tool_definitions

REPO_ROOT = Path(__file__).resolve().parents[3]
REFERENCE = REPO_ROOT / "docs/reference/composer-tools.md"


def _reference() -> str:
    """Return the reference with wrapping collapsed for stable phrase checks."""
    return " ".join(REFERENCE.read_text(encoding="utf-8").split())


def _tool_schema(name: str) -> dict[str, Any]:
    return next(definition["parameters"] for definition in get_tool_definitions() if definition["name"] == name)


def test_upsert_node_reference_tracks_structural_node_schema() -> None:
    reference = _reference()
    upsert = reference.split("### `upsert_node`", maxsplit=1)[1].split("### `upsert_edge`", maxsplit=1)[0]
    node_types = _tool_schema("upsert_node")["properties"]["node_type"]["enum"]

    for node_type in node_types:
        assert f"`{node_type}`" in upsert

    assert "| `branches` | array or object | No |" in upsert
    assert "List form is shorthand for an identity mapping" in upsert
    assert "object form maps each branch name to its input connection" in upsert
    assert "| `timeout_seconds` | number | No |" in upsert
    assert "finite positive structural-barrier timeout for `coalesce` or `row_union`" in upsert


def test_upsert_node_reference_states_row_union_authoring_contract() -> None:
    reference = _reference()
    upsert = reference.split("### `upsert_node`", maxsplit=1)[1].split("### `upsert_edge`", maxsplit=1)[0]

    assert "| `row_union` | `branches`, `on_success` |" in upsert
    assert "plugin-free, fixed `require_all` N-to-N barrier" in upsert
    assert "`input` must equal the first branch connection" in upsert
    assert "`on_success` must name a downstream processing connection, never a sink" in upsert
    assert "releases every original row unchanged in declared branch order" in upsert
    assert "does not accept `policy`, `merge`, `options`, or routing fields" in upsert


def test_reference_distinguishes_queue_row_union_and_coalesce() -> None:
    reference = _reference()

    assert "Not yet representable: queue fan-in" not in reference
    assert "**Structural fan-in choices.** Composer represents all three" in reference
    assert "A `queue` is a pass-through coordination point for multiple producers" in reference
    assert "does not wait for correlated fork branches or merge their payloads" in reference
    assert "A `row_union` reconverges correlated fork branches N-to-N" in reference
    assert "A `coalesce` reconverges correlated fork branches N-to-1" in reference


def test_set_pipeline_reference_includes_current_node_inventory() -> None:
    reference = _reference()
    set_pipeline = reference.split("### `set_pipeline`", maxsplit=1)[1].split("### `clear_source`", maxsplit=1)[0]

    for node_type in _tool_schema("upsert_node")["properties"]["node_type"]["enum"]:
        assert f"`{node_type}`" in set_pipeline
    assert "timeout_seconds?" in set_pipeline
