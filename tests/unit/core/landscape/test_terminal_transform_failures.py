"""Failure and discard COUNTS derive from terminal outcomes, not transform_errors rows (elspeth-5887fb7928).

Operator ruling 2026-09-23: a token counts as failed or discarded at a node
only when its terminal ``token_outcomes`` row says it failed on a transform
error, and only at the node whose error decided it. ``transform_errors`` rows
record attempts. They stay as evidence and never inflate a count.

Every case runs the four counting readers over one audit state built with the
real recorder writers: the web discard summary, the web failure categories,
and the MCP run summary and error analysis. The end-to-end batch cases
(fail-then-succeed on resume, a genuine discard, a routed batch) live in
``tests/integration/pipeline/test_batch_flush_recovery_and_redaction.py``.
The per-row twins are here: a per-row crash cannot be resumed end to end,
because the source is still ``loading`` when a row fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from elspeth.contracts import NodeStateStatus, NodeType
from elspeth.contracts.audit import DISCARD_SINK_NAME, TokenRef
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.contracts.errors import TransformErrorCategory, TransformErrorReason
from elspeth.core.landscape.schema import transform_errors_table
from elspeth.mcp.analyzers.reports import get_error_analysis, get_run_summary
from elspeth.web.execution.discard_summary import load_discard_summaries_from_db
from elspeth.web.execution.failure_samples import load_top_failure_categories
from tests.fixtures.landscape import (
    RecorderSetup,
    claim_test_work_item,
    leader_coordination_token,
    make_recorder_with_run,
    register_test_node,
)

_PLUGINS = {"xform": "mapper", "yform": "scorer"}


@dataclass(frozen=True, slots=True)
class _Counts:
    discarded: dict[str | None, int]
    categories: list[tuple[str, str, int]]
    run_summary_transform: int
    analysis_total: int
    analysis_by_plugin: dict[str, int]


def _setup(run_id: str) -> RecorderSetup:
    setup = make_recorder_with_run(run_id=run_id, source_node_id="src")
    for node_id, plugin in _PLUGINS.items():
        register_test_node(setup.data_flow, run_id, node_id, node_type=NodeType.TRANSFORM, plugin_name=plugin)
    return setup


def _token(setup: RecorderSetup, index: int) -> str:
    _row, token = setup.data_flow.create_row_with_token(
        coordination_token=leader_coordination_token(setup.factory, setup.run_id),
        source_node_id="src",
        row_index=index,
        data={"x": index},
        source_row_index=index,
        ingest_sequence=index,
    )
    return token.token_id


def _error(setup: RecorderSetup, token_id: str, node_id: str, *, reason: TransformErrorCategory, destination: str) -> None:
    """One attempt's transform_errors row, through the real writer."""
    member = leader_coordination_token(setup.factory, setup.run_id).membership
    work_item = claim_test_work_item(setup.factory, member_token=member, token_id=token_id, node_id=node_id)
    details: TransformErrorReason = {"reason": reason}
    setup.data_flow.record_transform_error(
        ref=TokenRef(token_id=token_id, run_id=setup.run_id),
        transform_id=node_id,
        row_data={"x": 0},
        error_details=details,
        destination=destination,
        member_token=member,
        work_item=work_item,
    )


def _terminal(setup: RecorderSetup, token_id: str, outcome: TerminalOutcome, path: TerminalPath, *, sink_name: str | None = None) -> None:
    error_hash = None if outcome is TerminalOutcome.SUCCESS else "a" * 16
    setup.data_flow.record_token_outcome_leader(
        coordination_token=leader_coordination_token(setup.factory, setup.run_id),
        ref=TokenRef(token_id=token_id, run_id=setup.run_id),
        outcome=outcome,
        path=path,
        sink_name=sink_name,
        error_hash=error_hash,
    )


def _completed_state(setup: RecorderSetup, token_id: str, node_id: str) -> None:
    """A later attempt that got the token past ``node_id``."""
    member = leader_coordination_token(setup.factory, setup.run_id).membership
    state = setup.execution.begin_node_state(token_id, node_id, 1, {"x": 0}, member_token=member, attempt=1)
    setup.execution.complete_node_state(
        state.state_id, NodeStateStatus.COMPLETED, output_data={"x": 0}, duration_ms=1.0, member_token=member
    )


def _error_created_order(setup: RecorderSetup, token_id: str) -> list[str]:
    """Control: the node order the writer stamped, oldest first, with no tie."""
    with setup.db.connection() as conn:
        rows = conn.execute(
            select(transform_errors_table.c.transform_id, transform_errors_table.c.created_at)
            .where(transform_errors_table.c.token_id == token_id)
            .order_by(transform_errors_table.c.created_at)
        ).all()
    stamps = [row.created_at for row in rows]
    assert len(set(stamps)) == len(stamps), f"control: the attempts need distinct created_at stamps, got {stamps}"
    return [row.transform_id for row in rows]


def _counts(setup: RecorderSetup) -> _Counts:
    summaries = load_discard_summaries_from_db(setup.db, [setup.run_id])
    discarded = (
        {stage.node_id: stage.count for stage in summaries[setup.run_id].stages if stage.stage == "transform_validation"}
        if setup.run_id in summaries
        else {}
    )
    categories = [(s.transform_id, s.category, s.count) for s in load_top_failure_categories(setup.db, setup.run_id)]
    run_summary: Any = get_run_summary(setup.db, setup.factory, setup.run_id)
    analysis: Any = get_error_analysis(setup.db, setup.factory, setup.run_id)
    return _Counts(
        discarded=discarded,
        categories=categories,
        run_summary_transform=run_summary["errors"]["transform"],
        analysis_total=analysis["transform_errors"]["total"],
        analysis_by_plugin={group["transform_plugin"]: group["count"] for group in analysis["transform_errors"]["by_transform"]},
    )


_NOTHING_FAILED = _Counts(discarded={}, categories=[], run_summary_transform=0, analysis_total=0, analysis_by_plugin={})


def test_a_token_whose_retried_attempt_was_delivered_is_neither_failed_nor_discarded() -> None:
    """Two attempts failed and were recorded, and the resumed third was delivered.

    No node state records the success here, which is the shape of a failed
    batch's non-triggering members. Only the terminal outcome says the token
    did not fail.
    """
    setup = _setup("delivered-after-retry")
    token = _token(setup, 0)
    _error(setup, token, "xform", reason="api_error", destination="discard")
    _error(setup, token, "xform", reason="api_error", destination="discard")
    _terminal(setup, token, TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW, sink_name="output")

    assert _counts(setup) == _NOTHING_FAILED


def test_a_token_counts_once_at_the_node_whose_error_decided_it() -> None:
    """An attempt failed at X, a resumed attempt passed X, and the token was then discarded at Y.

    The X row is evidence of an attempt the resume superseded. The token
    failed at Y and only at Y, with Y's category.
    """
    setup = _setup("x-then-y")
    token = _token(setup, 0)
    _error(setup, token, "xform", reason="api_error", destination="discard")
    _error(setup, token, "yform", reason="validation_failed", destination="discard")
    _terminal(setup, token, TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE)
    assert _error_created_order(setup, token) == ["xform", "yform"]

    assert _counts(setup) == _Counts(
        discarded={"yform": 1},
        categories=[("yform", "validation_failed", 1)],
        run_summary_transform=1,
        analysis_total=1,
        analysis_by_plugin={"scorer": 1},
    )


def test_the_category_is_the_deciding_attempts_not_every_attempts() -> None:
    """Two attempts at one node recorded different categories; the token failed once, under the last."""
    setup = _setup("two-categories")
    token = _token(setup, 0)
    _error(setup, token, "xform", reason="api_error", destination="discard")
    _error(setup, token, "xform", reason="validation_failed", destination="discard")
    _terminal(setup, token, TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE)
    assert _error_created_order(setup, token) == ["xform", "xform"]

    counts = _counts(setup)

    assert counts.categories == [("xform", "validation_failed", 1)]
    assert counts.discarded == {"xform": 1}
    assert counts.run_summary_transform == counts.analysis_total == 1


def test_a_per_row_discard_counts_as_discarded_and_a_routed_row_as_failed_only() -> None:
    """The per-row twin of the batch discard/routed pair.

    ``on_error: discard`` ends (FAILURE, QUARANTINED_AT_SOURCE) and is a
    discard. ``on_error: quarantine`` ends (FAILURE, ON_ERROR_ROUTED) at that
    sink. The row failed, but it was routed, not discarded.
    """
    setup = _setup("per-row-twin")
    discarded, routed = _token(setup, 0), _token(setup, 1)
    _error(setup, discarded, "xform", reason="api_error", destination="discard")
    _terminal(setup, discarded, TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE)
    _error(setup, routed, "xform", reason="api_error", destination="quarantine")
    _terminal(setup, routed, TerminalOutcome.FAILURE, TerminalPath.ON_ERROR_ROUTED, sink_name="quarantine")

    assert _counts(setup) == _Counts(
        discarded={"xform": 1},
        categories=[("xform", "api_error", 2)],
        run_summary_transform=2,
        analysis_total=2,
        analysis_by_plugin={"mapper": 2},
    )


def test_a_token_that_completed_the_node_did_not_fail_there() -> None:
    """A resumed attempt completed X, and a path that writes no transform error then quarantined the token.

    The collector and batch-member quarantines end (FAILURE,
    QUARANTINED_AT_SOURCE) without a transform_errors row. The token's latest
    row is still the superseded X attempt. The completed X state is what
    shows a transform error did not decide this token.
    """
    setup = _setup("completed-then-quarantined")
    token = _token(setup, 0)
    _error(setup, token, "xform", reason="api_error", destination="discard")
    _completed_state(setup, token, "xform")
    _terminal(setup, token, TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE)

    assert _counts(setup) == _NOTHING_FAILED


def test_a_failure_a_transform_error_did_not_decide_is_not_counted() -> None:
    """A recorded, superseded transform error, then a sink discard: the token failed at the sink, not at X."""
    setup = _setup("sink-discarded")
    token = _token(setup, 0)
    _error(setup, token, "xform", reason="api_error", destination="discard")
    _terminal(setup, token, TerminalOutcome.FAILURE, TerminalPath.SINK_DISCARDED, sink_name=DISCARD_SINK_NAME)

    counts = _counts(setup)

    assert counts.categories == []
    assert counts.discarded == {}
    assert counts.run_summary_transform == counts.analysis_total == 0
