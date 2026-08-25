"""ADR-019 MCP outcome distribution tests."""

from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import FrameKind, NodeType, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.identity import LineageFrame
from elspeth.mcp.analyzers.reports import get_outcome_analysis, get_run_summary
from tests.fixtures.landscape import make_recorder_with_run, register_test_node


def _record_token(
    setup_run_id: str,
    source_node_id: str,
    data_flow,
    *,
    row_index: int,
    outcome: TerminalOutcome | None,
    path: TerminalPath,
    **fields,
) -> None:
    row = data_flow.create_row(
        run_id=setup_run_id,
        source_node_id=source_node_id,
        row_index=row_index,
        data={"row": row_index},
        source_row_index=row_index,
        ingest_sequence=row_index,
    )
    token = data_flow.create_token(row.row_id)
    data_flow.record_token_outcome(
        ref=TokenRef(token_id=token.token_id, run_id=setup_run_id),
        outcome=outcome,
        path=path,
        **fields,
    )


def test_outcome_reports_group_by_path_not_lifecycle_only() -> None:
    setup = make_recorder_with_run(run_id="two-axis-report-run", source_node_id="source-0")
    register_test_node(
        setup.data_flow,
        setup.run_id,
        "sink-0",
        node_type=NodeType.SINK,
        plugin_name="csv_sink",
    )
    _record_token(
        setup.run_id,
        setup.source_node_id,
        setup.data_flow,
        row_index=0,
        outcome=TerminalOutcome.SUCCESS,
        path=TerminalPath.DEFAULT_FLOW,
        sink_name="sink-0",
    )
    _record_token(
        setup.run_id,
        setup.source_node_id,
        setup.data_flow,
        row_index=1,
        outcome=TerminalOutcome.SUCCESS,
        path=TerminalPath.FILTER_DROPPED,
    )
    setup.run_lifecycle.complete_run(setup.run_id, RunStatus.COMPLETED)

    outcome_analysis = get_outcome_analysis(setup.db, setup.factory, setup.run_id)
    run_summary = get_run_summary(setup.db, setup.factory, setup.run_id)

    for report in (outcome_analysis, run_summary):
        assert "error" not in report
        buckets = {(entry["outcome"], entry["path"], entry["completed"]): entry["count"] for entry in report["outcome_distribution"]}
        assert buckets[("success", "default_flow", True)] == 1
        assert buckets[("success", "filter_dropped", True)] == 1


def test_outcome_analysis_fork_and_join_counts_read_lineage_frames_and_tokens() -> None:
    """§4.1a: fork_operations counts DISTINCT fork-kind token_lineage_frames groups
    (never the retired token_outcomes.fork_group_id column); join_operations counts
    DISTINCT tokens.join_group_id (the surviving column, never token_outcomes)."""
    setup = make_recorder_with_run(run_id="fork-join-count-run", source_node_id="source-0")
    register_test_node(setup.data_flow, setup.run_id, "sink-0", node_type=NodeType.SINK, plugin_name="csv_sink")

    row = setup.data_flow.create_row(
        run_id=setup.run_id,
        source_node_id=setup.source_node_id,
        row_index=0,
        data={"row": 0},
        source_row_index=0,
        ingest_sequence=0,
    )

    # One fork group with two children — DISTINCT group_id at kind=fork must count
    # this as ONE fork operation, not two rows.
    branch_a = setup.data_flow.create_token(
        row.row_id,
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a"),),
    )
    branch_b = setup.data_flow.create_token(
        row.row_id,
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_b"),),
    )
    for token in (branch_a, branch_b):
        setup.data_flow.record_token_outcome(
            ref=TokenRef(token_id=token.token_id, run_id=setup.run_id),
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name="sink-0",
        )

    # A second, unrelated fork group — DISTINCT must count it separately (total 2).
    branch_c = setup.data_flow.create_token(
        row.row_id,
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-2", member_key="path_c"),),
    )
    setup.data_flow.record_token_outcome(
        ref=TokenRef(token_id=branch_c.token_id, run_id=setup.run_id),
        outcome=TerminalOutcome.SUCCESS,
        path=TerminalPath.DEFAULT_FLOW,
        sink_name="sink-0",
    )

    # One merged token carrying join_group_id — the crafted-token seam is legal here
    # because join_operations reads tokens.join_group_id directly, not a coalesce
    # transaction's derived state.
    merged = setup.data_flow.create_token(row.row_id, join_group_id="jg-1")
    setup.data_flow.record_token_outcome(
        ref=TokenRef(token_id=merged.token_id, run_id=setup.run_id),
        outcome=TerminalOutcome.SUCCESS,
        path=TerminalPath.DEFAULT_FLOW,
        sink_name="sink-0",
    )

    # One expand group with two members — expand_operations counts DISTINCT
    # expand-kind frame groups the same way fork_operations counts fork ones.
    member_1 = setup.data_flow.create_token(
        row.row_id,
        lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="m1"),),
    )
    member_2 = setup.data_flow.create_token(
        row.row_id,
        lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="m2"),),
    )
    for token in (member_1, member_2):
        setup.data_flow.record_token_outcome(
            ref=TokenRef(token_id=token.token_id, run_id=setup.run_id),
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name="sink-0",
        )

    setup.run_lifecycle.complete_run(setup.run_id, RunStatus.COMPLETED)

    outcome_analysis = get_outcome_analysis(setup.db, setup.factory, setup.run_id)
    assert "error" not in outcome_analysis
    assert outcome_analysis["summary"]["fork_operations"] == 2
    assert outcome_analysis["summary"]["join_operations"] == 1
    assert outcome_analysis["summary"]["expand_operations"] == 1
