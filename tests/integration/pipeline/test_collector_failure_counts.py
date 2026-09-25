"""Collector group failures are counted, categorised and analysed from their recorded verdicts (elspeth-5887fb7928 S2).

Operator ruling 2026-09-25: the group-verdict arm lives in the one counting
authority (``core/landscape/terminal_transform_failures.py``). It reads the
``collector_group_failures`` verdict rows (G, failed groups) and each failed
member's ``CollectorGroupFailure`` hold (M, failed member tokens). Every
counting reader takes both from there: the run status projection, the web run
accounting, the web failure categories, and the MCP run summary and error
analysis. The MCP ``errors.total`` is validation + transform + collector
member tokens.

Before this arm a failed collector group's members were invisible to the
categories and the error totals: Codex's collector counts probe recorded two
``(FAILURE, UNROUTED)`` members, an empty category list and
``errors.total == 0``.

Every case runs a real pipeline through the production build path, reading
the audit database the run wrote.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update

from elspeth.contracts import Determinism, PipelineRow
from elspeth.contracts.enums import NodeStateStatus, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    collector_group_failures_table,
    node_states_table,
    nodes_table,
    runs_table,
    token_outcomes_table,
    transform_errors_table,
)
from elspeth.mcp.analyzers.reports import get_error_analysis, get_run_summary
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.batch_stats import BatchStats
from elspeth.plugins.transforms.json_explode import JSONExplode
from elspeth.web.execution.accounting import load_run_accounting_from_db, load_run_accounting_map_from_db
from elspeth.web.execution.discard_summary import load_discard_summaries_from_db
from elspeth.web.execution.failure_samples import load_top_failure_categories
from tests.integration.mcp.test_group_failure_forensics import _run_depth3_failure
from tests.integration.pipeline.test_barrier_hold_payload import build_pipeline, resume_pipeline, run_pipeline
from tests.integration.pipeline.test_collector_failure_verdict import (
    _COLLECTOR_PIPELINE,
    _DOCS,
    _LOSSY_DOCS,
    _LOSSY_PIPELINE,
    _REAL_BATCH_STATS_PROCESS,
    _Crash,
    _flip_first_call,
    _inject,
)
from tests.integration.pipeline.test_row_type_violation_routing import _run_cli, _summing_collector_settings


@dataclass(frozen=True, slots=True)
class _Surfaces:
    """Every counting reader's view of one run."""

    categories: list[tuple[str, str, str, int]]
    errors: dict[str, int]
    groups_failed_mcp: int
    groups_failed_projection: int
    groups_failed_accounting: int
    collector_analysis: dict[str, Any]
    discarded: dict[str, Any]


def _surfaces(db: LandscapeDB, run_id: str) -> _Surfaces:
    factory = RecorderFactory(db)
    summary: Any = get_run_summary(db, factory, run_id)
    analysis: Any = get_error_analysis(db, factory, run_id)
    return _Surfaces(
        categories=[(s.kind, s.transform_id, s.category, s.count) for s in load_top_failure_categories(db, run_id, limit=10)],
        errors=dict(summary["errors"]),
        groups_failed_mcp=summary["counts"]["collector_groups_failed"],
        groups_failed_projection=factory.run_status_projection.count_failed_collector_groups(run_id),
        groups_failed_accounting=load_run_accounting_from_db(db, landscape_run_id=run_id).collector_groups_failed,
        collector_analysis=analysis["collector_group_failures"],
        discarded=dict(load_discard_summaries_from_db(db, [run_id])),
    )


def _only_run_id(db: LandscapeDB) -> str:
    with db.connection() as conn:
        return str(conn.execute(select(runs_table.c.run_id)).scalar_one())


def _collector_node(db: LandscapeDB) -> str:
    with db.connection() as conn:
        return str(conn.execute(select(nodes_table.c.node_id).where(nodes_table.c.node_type == "collector")).scalar_one())


def _failed_unrouted(db: LandscapeDB, run_id: str) -> int:
    with db.connection() as conn:
        return int(
            conn.execute(
                select(func.count())
                .select_from(token_outcomes_table)
                .where(token_outcomes_table.c.run_id == run_id)
                .where(token_outcomes_table.c.completed == 1)
                .where(token_outcomes_table.c.outcome == TerminalOutcome.FAILURE.value)
                .where(token_outcomes_table.c.path == TerminalPath.UNROUTED.value)
            ).scalar_one()
        )


def _groups_by_direct_count(db: LandscapeDB, run_id: str) -> int:
    """The count the three rewired sites made before S2, kept as the regression control."""
    with db.connection() as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(collector_group_failures_table).where(collector_group_failures_table.c.run_id == run_id)
            ).scalar_one()
        )


def _analysis(plugin: str, node: str, reason: str, *, groups: int, members: int) -> dict[str, Any]:
    return {
        "groups_total": groups,
        "member_tokens_total": members,
        "by_collector": [
            {"collector_plugin": plugin, "node_id": node, "failure_reason": reason, "groups": groups, "member_tokens": members}
        ],
    }


def test_codex_collector_counts_probe_now_counts_the_failed_members(tmp_path: Path) -> None:
    """Codex's probe shape through ``elspeth run``: a collector's non-canonical sum fails its group of two.

    Before the arm: categories ``[]`` and ``errors.total == 0`` beside two
    ``(FAILURE, UNROUTED)`` members.
    """
    cli = _run_cli(tmp_path, _summing_collector_settings(tmp_path))
    assert cli.exit_code == 1, cli.output
    assert "Traceback" not in cli.output
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    run_id = _only_run_id(db)
    node = _collector_node(db)
    assert _failed_unrouted(db, run_id) == 2, "control: the group's two members ended (FAILURE, UNROUTED)"

    surfaces = _surfaces(db, run_id)

    assert surfaces.categories == [("collector_group", node, "collector_contract_violation", 2)]
    assert surfaces.errors == {"validation": 0, "transform": 0, "collector_group": 2, "total": 2}
    assert surfaces.groups_failed_mcp == surfaces.groups_failed_projection == surfaces.groups_failed_accounting == 1
    assert surfaces.collector_analysis == _analysis("batch_stats", node, "collector_contract_violation", groups=1, members=2)
    assert surfaces.discarded == {}, "a failed collector member is failed, not discarded"


@pytest.mark.parametrize(
    ("arm", "reason", "members", "transform"),
    [
        ("returned_error", "collector_transform_error", 3, 0),
        ("noncanonical", "collector_contract_violation", 3, 0),
        ("lost_members", "collector_missing_members", 2, 1),
    ],
)
def test_each_failure_arm_counts_its_members_once_under_its_recorded_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, reason: str, members: int, transform: int
) -> None:
    """The two plugin arms (three members) and the lost-members arm (two arrived, one discarded upstream)."""
    if arm == "lost_members":
        env = build_pipeline(tmp_path, _LOSSY_PIPELINE, _LOSSY_DOCS)
    else:
        env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, _DOCS)
        _flip_first_call(monkeypatch, arm)
    result = run_pipeline(env)
    db = env["db"]
    node = _collector_node(db)
    assert _failed_unrouted(db, result.run_id) == members, "control: every group member ended (FAILURE, UNROUTED)"

    surfaces = _surfaces(db, result.run_id)

    expected_categories = [("collector_group", node, reason, members)]
    if transform:
        # The divide-by-zero member was discarded by the value_transform: the
        # transform arm, not the group arm.
        with db.connection() as conn:
            invert = str(conn.execute(select(nodes_table.c.node_id).where(nodes_table.c.plugin_name == "value_transform")).scalar_one())
        expected_categories.append(("transform_error", invert, "invalid_input", 1))
    assert surfaces.categories == expected_categories
    assert surfaces.errors == {"validation": 0, "transform": transform, "collector_group": members, "total": transform + members}
    assert surfaces.groups_failed_mcp == surfaces.groups_failed_projection == surfaces.groups_failed_accounting == 1
    assert surfaces.groups_failed_projection == result.collector_groups_failed
    assert surfaces.collector_analysis == _analysis("batch_stats", node, reason, groups=1, members=members)


def test_a_zero_member_group_is_one_failed_group_with_no_member_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``empty_expansion`` under require_all: G = 1 and M = 0. The group shows in error analysis with member_tokens 0."""
    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, [{"id": 1, "items": []}])

    def emit_empty(self: JSONExplode, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        return TransformResult.success_empty(success_reason={"action": "transformed"})

    monkeypatch.setattr(JSONExplode, "passes_through_input", True)
    monkeypatch.setattr(JSONExplode, "can_drop_rows", True)
    monkeypatch.setattr(JSONExplode, "process", emit_empty)
    result = run_pipeline(env)
    db = env["db"]
    node = _collector_node(db)

    surfaces = _surfaces(db, result.run_id)

    assert surfaces.categories == []
    assert surfaces.errors == {"validation": 0, "transform": 0, "collector_group": 0, "total": 0}
    assert surfaces.groups_failed_mcp == surfaces.groups_failed_projection == surfaces.groups_failed_accounting == 1
    assert surfaces.collector_analysis == _analysis("batch_stats", node, "empty_expansion", groups=1, members=0)


def test_a_member_carrying_a_stale_transform_error_counts_once_in_the_group_arm(tmp_path: Path) -> None:
    """A failed group's member with an earlier attempt's transform_errors row is one failed token, not two.

    The lost-members run leaves two arrived members and one discarded one.
    One arrived member is then given a transform_errors row at the upstream
    value_transform, the attempt evidence a retried and superseded attempt
    leaves. The transform arm admits only the two paths a transform error
    disposes onto, so the member's UNROUTED terminal keeps it in the group arm.
    """
    env = build_pipeline(tmp_path, _LOSSY_PIPELINE, _LOSSY_DOCS)
    result = run_pipeline(env)
    db = env["db"]
    before = _surfaces(db, result.run_id)
    with db.connection() as conn:
        [member_state] = (
            conn.execute(
                select(node_states_table.c.token_id)
                .where(node_states_table.c.node_id == _collector_node(db))
                .where(node_states_table.c.error_json.like('%"CollectorGroupFailure"%'))
                .limit(1)
            )
            .scalars()
            .all()
        )
        invert = conn.execute(select(nodes_table.c.node_id).where(nodes_table.c.plugin_name == "value_transform")).scalar_one()
    with db.engine.begin() as conn:
        conn.execute(
            insert(transform_errors_table).values(
                error_id="terr_stale_attempt_of_a_member",
                run_id=result.run_id,
                token_id=member_state,
                transform_id=invert,
                row_hash="a" * 64,
                row_data_json="{}",
                error_details_json=json.dumps({"reason": "api_error"}),
                destination="discard",
                created_at=datetime.now(UTC),
            )
        )
    with db.connection() as conn:
        rows_for_member = conn.execute(
            select(func.count()).select_from(transform_errors_table).where(transform_errors_table.c.token_id == member_state)
        ).scalar_one()
    assert rows_for_member == 1, "control: the member now carries a transform_errors row"

    after = _surfaces(db, result.run_id)

    assert after == before
    assert after.errors == {"validation": 0, "transform": 1, "collector_group": 2, "total": 3}


def test_the_window_between_a_recorded_verdict_and_its_terminals_counts_the_group_but_no_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the verdict committed, before any member terminal: G = 1, M = 0; resume then counts all three.

    No member has terminally failed yet, so none counts (the terminal-outcome
    ruling). The holds already say FAILED; a reader that trusted them without
    the terminal would count three members of a run that has not decided them.
    """
    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, _DOCS)
    _flip_first_call(monkeypatch, "returned_error")
    with monkeypatch.context() as crash_patch:
        fired = _inject(crash_patch, "after_verdict")
        with pytest.raises(_Crash):
            run_pipeline(env)
    assert fired == ["after_verdict"]
    db = env["db"]
    run_id = _only_run_id(db)
    node = _collector_node(db)
    with db.connection() as conn:
        failed_holds = conn.execute(
            select(func.count())
            .select_from(node_states_table)
            .where(node_states_table.c.node_id == node)
            .where(node_states_table.c.status == NodeStateStatus.FAILED.value)
            .where(node_states_table.c.error_json.like('%"CollectorGroupFailure"%'))
        ).scalar_one()
    assert failed_holds == 3, "control: the verdict failed all three holds"
    assert _failed_unrouted(db, run_id) == 0, "control: no member terminal was written"

    crashed = _surfaces(db, run_id)

    assert crashed.categories == []
    assert crashed.errors["collector_group"] == 0
    assert crashed.groups_failed_mcp == crashed.groups_failed_projection == 1
    assert crashed.collector_analysis == _analysis("batch_stats", node, "collector_transform_error", groups=1, members=0)

    resume_pipeline(env)
    resumed = _surfaces(db, run_id)

    assert resumed.categories == [("collector_group", node, "collector_transform_error", 3)]
    assert resumed.errors["collector_group"] == 3
    assert resumed.collector_analysis == _analysis("batch_stats", node, "collector_transform_error", groups=1, members=3)


class _FlushFault(Exception):
    """The plugin raising mid-flush: the flush state fails, the holds stay OPEN."""


def test_a_flush_that_raised_and_then_succeeded_on_resume_counts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The FAILED flush state is attempt evidence. The resumed flush released the group, so no count moves."""
    monkeypatch.setattr(BatchStats, "determinism", Determinism.NON_DETERMINISTIC)
    calls: list[int] = []

    def raise_first(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        calls.append(len(rows))
        if len(calls) == 1:
            raise _FlushFault("transient fault inside the collector flush")
        return _REAL_BATCH_STATS_PROCESS(self, rows, ctx)

    monkeypatch.setattr(BatchStats, "process", raise_first)
    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, _DOCS)
    with pytest.raises(_FlushFault):
        run_pipeline(env)
    db = env["db"]
    node = _collector_node(db)
    with db.connection() as conn:
        statuses = sorted(
            str(status) for status in conn.execute(select(node_states_table.c.status).where(node_states_table.c.node_id == node)).scalars()
        )
    assert statuses == [NodeStateStatus.FAILED.value] + [NodeStateStatus.OPEN.value] * 3, (
        "control: a FAILED flush state and three OPEN holds"
    )

    resumed = resume_pipeline(env)

    assert resumed.status is RunStatus.COMPLETED
    assert _surfaces(db, resumed.run_id) == _Surfaces(
        categories=[],
        errors={"validation": 0, "transform": 0, "collector_group": 0, "total": 0},
        groups_failed_mcp=0,
        groups_failed_projection=0,
        groups_failed_accounting=0,
        collector_analysis={"groups_total": 0, "member_tokens_total": 0, "by_collector": []},
        discarded={},
    )


def test_nested_groups_count_each_member_once_at_its_own_collector(tmp_path: Path) -> None:
    """The depth-3 failure: an inner and an outer collector group fail; G = 2 and each member counts once.

    The discarded sentence is a transform failure (the transform arm). The
    coalesce survivor also ends (FAILURE, UNROUTED) but holds no collector
    verdict: a coalesce group failure is not counted by name (the named gap in
    the authority's docstring), so it is in neither arm.
    """
    url, run_id = _run_depth3_failure(tmp_path)
    db = LandscapeDB(url)
    try:
        with db.connection() as conn:
            plugins = {
                str(row.node_id): str(row.plugin_name)
                for row in conn.execute(
                    select(nodes_table.c.node_id, nodes_table.c.plugin_name)
                    .where(nodes_table.c.run_id == run_id)
                    .where(nodes_table.c.node_type == "collector")
                ).all()
            }
        assert len(plugins) == 2, "control: an inner and an outer collector"
        assert _failed_unrouted(db, run_id) == 3, "control: two collector members and the coalesce survivor ended UNROUTED"

        surfaces = _surfaces(db, run_id)
    finally:
        db.close()

    collector_categories = sorted(entry for entry in surfaces.categories if entry[0] == "collector_group")
    assert collector_categories == sorted(("collector_group", node, "collector_missing_members", 1) for node in plugins)
    assert [entry[2:] for entry in surfaces.categories if entry[0] == "transform_error"] == [("invalid_input", 1)]
    assert surfaces.errors == {"validation": 0, "transform": 1, "collector_group": 2, "total": 3}
    assert surfaces.groups_failed_mcp == surfaces.groups_failed_projection == surfaces.groups_failed_accounting == 2
    assert surfaces.collector_analysis == {
        "groups_total": 2,
        "member_tokens_total": 2,
        "by_collector": [
            {
                "collector_plugin": plugins[node],
                "node_id": node,
                "failure_reason": "collector_missing_members",
                "groups": 1,
                "member_tokens": 1,
            }
            for node in sorted(plugins)
        ],
    }


def _readers(db: LandscapeDB, run_id: str) -> list[Any]:
    factory = RecorderFactory(db)
    return [
        lambda: load_top_failure_categories(db, run_id),
        lambda: get_run_summary(db, factory, run_id),
        lambda: get_error_analysis(db, factory, run_id),
    ]


@pytest.mark.parametrize(
    ("tamper", "match"),
    [
        ("hold_reason", "whose verdict records 'collector_transform_error'"),
        ("group_reason", "whose verdict records 'collector_missing_members'"),
        ("hold_group", "which has no collector_group_failures row"),
        ("hold_without_group", "does not carry the verdict's failure_reason, group_id"),
    ],
)
def test_a_hold_that_disagrees_with_its_group_verdict_is_refused_by_every_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str, match: str
) -> None:
    """The verdict row and its holds are one transaction; a disagreement between them is corruption, never a count."""
    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, _DOCS)
    _flip_first_call(monkeypatch, "returned_error")
    result = run_pipeline(env)
    db = env["db"]
    assert _surfaces(db, result.run_id).errors["collector_group"] == 3, "control: the untampered run counts"
    with db.connection() as conn:
        hold_id, error_json = conn.execute(
            select(node_states_table.c.state_id, node_states_table.c.error_json)
            .where(node_states_table.c.error_json.like('%"CollectorGroupFailure"%'))
            .order_by(node_states_table.c.state_id)
            .limit(1)
        ).one()
    error = json.loads(error_json)
    with db.engine.begin() as conn:
        if tamper == "group_reason":
            conn.execute(update(collector_group_failures_table).values(failure_reason="collector_missing_members"))
        else:
            if tamper == "hold_reason":
                error["context"]["failure_reason"] = "collector_missing_members"
            elif tamper == "hold_group":
                error["context"]["group_id"] = "0" * 32
            else:
                del error["context"]["group_id"]
            conn.execute(update(node_states_table).where(node_states_table.c.state_id == hold_id).values(error_json=json.dumps(error)))

    for read in _readers(db, result.run_id):
        with pytest.raises(AuditIntegrityError, match=match):
            read()


def test_counts_are_per_run_when_two_runs_share_node_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same pipeline twice into one audit database: identical node ids, one failed run, one clean run."""
    db = LandscapeDB(f"sqlite:///{tmp_path / 'shared.db'}")
    try:
        _flip_first_call(monkeypatch, "returned_error")  # the first flush fails, every later one succeeds
        failed = run_pipeline(build_pipeline(tmp_path / "first", _COLLECTOR_PIPELINE, _DOCS, db=db))
        clean = run_pipeline(build_pipeline(tmp_path / "second", _COLLECTOR_PIPELINE, _DOCS, db=db))
        with db.connection() as conn:
            node_ids = conn.execute(select(nodes_table.c.run_id, nodes_table.c.node_id).where(nodes_table.c.node_type == "collector")).all()
        assert {run for run, _node in node_ids} == {failed.run_id, clean.run_id}
        assert len({node for _run, node in node_ids}) == 1, "control: both runs share the collector node id"
        assert failed.status is RunStatus.FAILED
        assert clean.status is RunStatus.COMPLETED

        failed_surfaces = _surfaces(db, failed.run_id)
        clean_surfaces = _surfaces(db, clean.run_id)
        batch = load_run_accounting_map_from_db(db, [failed.run_id, clean.run_id])
    finally:
        db.close()

    assert failed_surfaces.errors["collector_group"] == 3
    assert failed_surfaces.groups_failed_projection == 1
    assert clean_surfaces.errors == {"validation": 0, "transform": 0, "collector_group": 0, "total": 0}
    assert clean_surfaces.groups_failed_mcp == clean_surfaces.groups_failed_projection == clean_surfaces.groups_failed_accounting == 0
    assert clean_surfaces.categories == []
    assert {run: accounting.collector_groups_failed for run, accounting in batch.accounting.items()} == {failed.run_id: 1, clean.run_id: 0}


def test_a_value_in_a_collector_error_reason_reaches_no_counting_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A collector plugin whose returned error quotes a row value: only the reason code leaves the readers.

    The positive control: the value IS in the audit trail (the flush state's
    scrubbed, structured reason), so the readers had it and did not render it.
    """
    sentinel = "SENTINEL-ROW-VALUE-5887-S2"
    monkeypatch.setattr(BatchStats, "determinism", Determinism.NON_DETERMINISTIC)

    def quote_the_value(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        return TransformResult.error({"reason": "invalid_input", "message": f"cannot sum {sentinel}"}, retryable=False)

    monkeypatch.setattr(BatchStats, "process", quote_the_value)
    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, _DOCS)
    result = run_pipeline(env)
    db = env["db"]
    with db.connection() as conn:
        flush_errors = [
            json.loads(error_json)
            for error_json in conn.execute(
                select(node_states_table.c.error_json).where(node_states_table.c.error_json.like('%"TransformError"%'))
            ).scalars()
        ]
    assert [error["context"]["message"] for error in flush_errors] == [f"cannot sum {sentinel}"], "control: the value is in the audit trail"
    assert flush_errors[0]["exception"] == "Collector transform 'batch_stats' returned an error", "the message is not the reason flattened"

    factory = RecorderFactory(db)
    rendered = [
        repr(load_top_failure_categories(db, result.run_id)),
        repr(get_run_summary(db, factory, result.run_id)),
        repr(get_error_analysis(db, factory, result.run_id)),
    ]
    assert "collector_transform_error" in rendered[0], "control: the categories name the group failure"
    assert not [surface for surface in rendered if sentinel in surface]


@pytest.mark.parametrize("shape", ["transform_failures_only", "collector_failure", "zero_member_group", "clean"])
def test_the_rewired_group_counts_equal_the_direct_count_they_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    """Regression control: the three sites now count through the authority and report what the direct count did.

    ``transform_failures_only`` has no collector at all: the runs every
    earlier release reported, whose numbers must not move, including an MCP
    ``errors.total`` that is still validation + transform.
    """
    if shape == "transform_failures_only":
        body = """
transforms:
  - name: invert
    plugin: value_transform
    input: buffered
    on_success: out
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: inverse
          expression: "1000 / row['id']"
"""
        env = build_pipeline(tmp_path, body, [{"id": 0}, {"id": 4}, {"id": 0}])
    elif shape == "zero_member_group":
        env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, [{"id": 1, "items": []}])

        def emit_empty(self: JSONExplode, row: PipelineRow, ctx: PluginContext) -> TransformResult:
            return TransformResult.success_empty(success_reason={"action": "transformed"})

        monkeypatch.setattr(JSONExplode, "passes_through_input", True)
        monkeypatch.setattr(JSONExplode, "can_drop_rows", True)
        monkeypatch.setattr(JSONExplode, "process", emit_empty)
    else:
        env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, _DOCS)
        if shape == "collector_failure":
            _flip_first_call(monkeypatch, "returned_error")
    result = run_pipeline(env)
    db = env["db"]
    direct = _groups_by_direct_count(db, result.run_id)

    surfaces = _surfaces(db, result.run_id)

    assert surfaces.groups_failed_mcp == surfaces.groups_failed_projection == surfaces.groups_failed_accounting == direct
    assert result.collector_groups_failed == direct
    if shape == "transform_failures_only":
        assert direct == 0
        assert surfaces.errors == {"validation": 0, "transform": 2, "collector_group": 0, "total": 2}


def test_a_failed_collector_state_that_is_not_a_verdict_hold_is_not_a_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hold type is the discriminator, not the FAILED status and not the UNROUTED terminal.

    One member's hold is rewritten as another kind of FAILED collector state
    (the type a quarantined member of a successful flush records). Its token
    still ended (FAILURE, UNROUTED) at the same node, and it no longer counts.
    """
    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, _DOCS)
    _flip_first_call(monkeypatch, "returned_error")
    result = run_pipeline(env)
    db = env["db"]
    node = _collector_node(db)
    with db.connection() as conn:
        hold_id = conn.execute(
            select(node_states_table.c.state_id)
            .where(node_states_table.c.error_json.like('%"CollectorGroupFailure"%'))
            .order_by(node_states_table.c.state_id)
            .limit(1)
        ).scalar_one()
    other_failure = {"exception": "quarantined_in_group", "phase": "collector_flush", "type": "CollectorMemberQuarantine"}
    with db.engine.begin() as conn:
        conn.execute(update(node_states_table).where(node_states_table.c.state_id == hold_id).values(error_json=json.dumps(other_failure)))
    assert _failed_unrouted(db, result.run_id) == 3, "control: all three members still ended (FAILURE, UNROUTED)"

    surfaces = _surfaces(db, result.run_id)

    assert surfaces.categories == [("collector_group", node, "collector_transform_error", 2)]
    assert surfaces.errors["collector_group"] == 2
    assert surfaces.groups_failed_projection == 1
