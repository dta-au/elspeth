"""Group/lineage read surface of the Landscape MCP analyzer (spec §9 row 6, ADR-042 D3).

`list_group_records` / `list_group_losses` / `get_token_lineage` are the
operator's reconstruction surface: records by group, losses by
(closer, group), lineage path per token. `list_tokens`' frames-derived
projection is pinned in `test_analyzer_queries.py`; these tests own the
three new functions and their facade/tool plumbing.
"""

from __future__ import annotations

from pathlib import Path

from elspeth.core.landscape.database import LandscapeDB
from elspeth.mcp.analyzer import LandscapeAnalyzer
from elspeth.mcp.analyzers import queries
from elspeth.mcp.server import _TOOLS
from tests.fixtures.group_lineage import (
    EXPAND_GROUP,
    FORK_GROUP,
    RUN_ID,
    make_seeded_db_and_factory,
    seed_expand_frame,
    seed_expand_group,
    seed_fork_member,
    seed_loss,
    seed_run,
)


def test_list_group_records_returns_expand_roster_facts() -> None:
    db, factory = make_seeded_db_and_factory()
    seed_expand_group(db, member_count=3)
    records = queries.list_group_records(db, factory, RUN_ID)
    assert [(r["group_id"], r["kind"], r["member_count"]) for r in records] == [(EXPAND_GROUP, "expand", 3)]
    assert records[0]["opener_token_id"] == "tok-opener"
    assert records[0]["created_at"] is not None


def test_list_group_records_kind_filter_excludes_other_kinds() -> None:
    db, factory = make_seeded_db_and_factory()
    seed_expand_group(db, member_count=1)
    assert queries.list_group_records(db, factory, RUN_ID, kind="fork") == []
    assert [r["group_id"] for r in queries.list_group_records(db, factory, RUN_ID, kind="expand")] == [EXPAND_GROUP]


def test_list_group_losses_projects_the_full_ledger_row() -> None:
    db, factory = make_seeded_db_and_factory()
    seed_fork_member(db, token_id="tok-b", member_key="path_b")
    seed_loss(db, member_key="path_b", token_id="tok-b", adopted_epoch=None)
    losses = queries.list_group_losses(db, factory, RUN_ID)
    assert len(losses) == 1
    loss = losses[0]
    assert loss["closer_name"] == "merger"
    assert loss["group_id"] == FORK_GROUP
    assert loss["member_key"] == "path_b"
    assert loss["token_id"] == "tok-b"
    assert loss["reason"] == "quarantined"
    assert loss["recorded_by"] == "worker:test"
    assert loss["adopted_epoch"] is None


def test_list_group_losses_reads_the_full_ledger_adopted_or_not() -> None:
    """§6.2 full-table-read discipline on the wire too: adoption is a leader
    cursor, never a truth filter — both rows must surface."""
    db, factory = make_seeded_db_and_factory()
    seed_fork_member(db, token_id="tok-a", member_key="path_a")
    seed_fork_member(db, token_id="tok-b", member_key="path_b")
    seed_loss(db, member_key="path_a", token_id="tok-a", adopted_epoch=7)
    seed_loss(db, member_key="path_b", token_id="tok-b", adopted_epoch=None)
    losses = queries.list_group_losses(db, factory, RUN_ID)
    assert {(loss["member_key"], loss["adopted_epoch"]) for loss in losses} == {("path_a", 7), ("path_b", None)}


def test_list_group_losses_group_filter() -> None:
    db, factory = make_seeded_db_and_factory()
    seed_fork_member(db, token_id="tok-a", member_key="path_a", group_id="fg-other")
    seed_fork_member(db, token_id="tok-b", member_key="path_b")
    seed_loss(db, member_key="path_a", token_id="tok-a", adopted_epoch=None, group_id="fg-other")
    seed_loss(db, member_key="path_b", token_id="tok-b", adopted_epoch=None)
    losses = queries.list_group_losses(db, factory, RUN_ID, group_id=FORK_GROUP)
    assert [(loss["group_id"], loss["member_key"]) for loss in losses] == [(FORK_GROUP, "path_b")]


def test_get_token_lineage_is_depth_ordered_outermost_first() -> None:
    db, factory = make_seeded_db_and_factory()
    seed_fork_member(db, token_id="tok-nested", member_key="path_a")  # depth 0 FORK frame
    # add a depth-1 EXPAND frame on the same token (fork-then-expand nesting)
    seed_expand_frame(db, token_id="tok-nested", depth=1, group_id=EXPAND_GROUP)
    frames = queries.get_token_lineage(db, factory, RUN_ID, "tok-nested")
    assert [f["depth"] for f in frames] == [0, 1]
    assert [f["kind"] for f in frames] == ["fork", "expand"]
    assert frames[0] == {"depth": 0, "kind": "fork", "group_id": FORK_GROUP, "member_key": "path_a"}
    assert frames[1]["member_key"] == "tok-nested"


def test_get_token_lineage_unknown_token_is_empty() -> None:
    db, factory = make_seeded_db_and_factory()
    assert queries.get_token_lineage(db, factory, RUN_ID, "tok-ghost") == []


def test_analyzer_facade_forwards_the_three_group_surfaces(tmp_path: Path) -> None:
    """Through the REAL read-only facade (LandscapeAnalyzer opens by URL with
    RecorderFactory.read_only) — the surface the live MCP server runs on."""
    db_path = tmp_path / "audit.db"
    db = LandscapeDB(f"sqlite:///{db_path}")
    seed_run(db)
    members = seed_expand_group(db, member_count=2)
    seed_loss(db, member_key=members[0], token_id=members[0], adopted_epoch=None, closer_name="page_stitcher", group_id=EXPAND_GROUP)
    db.close()

    analyzer = LandscapeAnalyzer(f"sqlite:///{db_path}")
    try:
        assert [r["group_id"] for r in analyzer.list_group_records(RUN_ID, kind="expand")] == [EXPAND_GROUP]
        assert [loss["closer_name"] for loss in analyzer.list_group_losses(RUN_ID, group_id=EXPAND_GROUP)] == ["page_stitcher"]
        assert [f["group_id"] for f in analyzer.get_token_lineage(RUN_ID, members[0])] == [EXPAND_GROUP]
    finally:
        analyzer.close()


def test_the_three_group_tools_are_registered_with_schemas() -> None:
    for tool_name, required in (
        ("list_group_records", {"run_id"}),
        ("list_group_losses", {"run_id"}),
        ("get_token_lineage", {"run_id", "token_id"}),
    ):
        tool = _TOOLS[tool_name]
        assert required <= set(tool.schema_properties)
        assert tool.description
