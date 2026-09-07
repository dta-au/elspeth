"""Column-presence and live-write smoke tests for audit-story fields."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from elspeth.core.landscape.schema import runs_table
from tests.fixtures.landscape import make_factory, make_landscape_db
from tests.helpers.tree_gate import iter_gate_files


def test_runs_table_has_audit_story_columns() -> None:
    expected_new = {"llm_call_count", "seeded_from_cache", "cache_key"}
    actual = {col.name for col in runs_table.columns}
    assert not expected_new - actual
    assert "started_at" in actual
    assert "source_data_hash" not in actual
    assert "plugin_versions" not in actual


def test_llm_call_count_is_nullable_integer() -> None:
    assert runs_table.c.llm_call_count.nullable


def test_seeded_from_cache_has_server_default() -> None:
    col = runs_table.c.seeded_from_cache
    assert not col.nullable
    assert col.server_default is not None


def test_live_begin_run_writes_non_cache_defaults() -> None:
    db = make_landscape_db()
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(
        config={"pipeline": "test"},
        canonical_version="v1",
        run_id="run-live",
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    assert run.llm_call_count is None
    assert run.seeded_from_cache is False
    assert run.cache_key is None
    with db.connection() as conn:
        row = conn.execute(select(runs_table).where(runs_table.c.run_id == "run-live")).one()
    assert row.llm_call_count is None
    assert row.seeded_from_cache is False
    assert row.cache_key is None


def test_landscape_core_keeps_web_session_identifiers_out_of_audit_schema() -> None:
    """Landscape joins to web runs without embedding web-session identity."""
    landscape_root = Path(__file__).parents[4] / "src" / "elspeth" / "core" / "landscape"
    forbidden = ("session_id", "chat_message_id", "composition_state_id")
    hits: list[str] = []
    for path in iter_gate_files(landscape_root):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(landscape_root)}:{token}")
    assert hits == []
