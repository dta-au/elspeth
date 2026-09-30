"""Public completion refuses inconsistent verdicts without changing durable audit rows."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import delete, select, update

from elspeth.contracts import AggregationResultMember, BatchStatus, NodeType, OutputMode, TriggerType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import AggregationMemberAction
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.node_state_context import AggregationFlushContext
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.data_flow.outcomes import record_buffered_outcome_guarded
from elspeth.core.landscape.schema import (
    aggregation_result_members_table,
    aggregation_result_outputs_table,
    aggregation_results_table,
    batch_members_table,
    batches_table,
    node_states_table,
)
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run, register_test_node
from tests.unit.core.landscape.test_aggregation_result_receipt import _add_batch_member


@dataclass(frozen=True)
class Verdict:
    mode: OutputMode
    rows: tuple[PipelineRow, ...]
    shape: str
    members: tuple[AggregationResultMember, ...]
    parent: str | None
    output_hash: str
    state_id: str = "state-1"
    batch_id: str = "batch-1"
    node_id: str = "agg-1"


@pytest.fixture
def recorder_setup() -> Iterator[RecorderSetup]:
    setup = make_recorder_with_run(run_id="run-1", source_node_id="source-0", source_plugin_name="csv")
    try:
        yield setup
    finally:
        setup.db.close()


def _prepare(setup: RecorderSetup) -> tuple[RecorderSetup, Verdict]:
    register_test_node(setup.data_flow, setup.run_id, "agg-1", node_type=NodeType.AGGREGATION, plugin_name="aggregator")
    for ordinal in range(2):
        setup.data_flow.create_row_with_token(
            setup.source_node_id,
            ordinal,
            {"value": ordinal + 1},
            row_id=f"row-{ordinal}",
            source_row_index=ordinal,
            ingest_sequence=ordinal,
            coordination_token=setup.coordination_token,
            token_id=f"tok-{ordinal}",
        )
    setup.execution.begin_node_state(
        "tok-0", "agg-1", 0, {"value": 1}, state_id="state-1", member_token=setup.coordination_token.membership
    )
    setup.execution.create_batch("agg-1", batch_id="batch-1", coordination_token=setup.coordination_token)
    for ordinal in range(2):
        _add_batch_member(setup, "batch-1", f"tok-{ordinal}", ordinal)
    with setup.db.write_connection() as conn:
        for ordinal in range(2):
            record_buffered_outcome_guarded(
                conn, run_id=setup.run_id, token_id=f"tok-{ordinal}", batch_id="batch-1", recorded_at=datetime.now(UTC)
            )
    setup.execution.update_batch_status("batch-1", BatchStatus.EXECUTING, state_id="state-1", coordination_token=setup.coordination_token)
    row = PipelineRow({"total": 3}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
    members = tuple(
        AggregationResultMember(TokenRef(token_id=f"tok-{ordinal}", run_id=setup.run_id), AggregationMemberAction.CONSUME_BATCH, None)
        for ordinal in range(2)
    )
    return setup, Verdict(OutputMode.TRANSFORM, (row,), "single", members, "tok-0", stable_hash(row.to_dict()))


def _complete(setup: RecorderSetup, verdict: Verdict) -> object:
    return setup.execution.complete_aggregation_result(
        batch_id=verdict.batch_id,
        aggregation_node_id=verdict.node_id,
        state_id=verdict.state_id,
        trigger_type=TriggerType.END_OF_SOURCE,
        output_mode=verdict.mode,
        output_rows=verdict.rows,
        output_shape=verdict.shape,
        output_hash=verdict.output_hash,
        members=verdict.members,
        expansion_parent_token_id=verdict.parent,
        duration_ms=1.0,
        success_reason=None,
        context_after=AggregationFlushContext(
            trigger_type=TriggerType.END_OF_SOURCE.value,
            buffer_size=2,
            batch_id=verdict.batch_id,
            flush_index=1,
            rows_seen_total=2,
            row_start=1,
            row_end=2,
            is_end_of_source=True,
        ),
        coordination_token=setup.coordination_token,
    )


def _durable(setup: RecorderSetup) -> tuple[tuple[tuple[object, ...], ...], ...]:
    with setup.db.read_only_connection() as conn:
        return tuple(
            tuple(tuple(row) for row in conn.execute(select(table)).all())
            for table in (
                node_states_table,
                batches_table,
                batch_members_table,
                aggregation_results_table,
                aggregation_result_outputs_table,
                aggregation_result_members_table,
            )
        )


@pytest.mark.parametrize(
    "defect, message",
    [
        ("nominal-mode", "nominal OutputMode"),
        ("empty-members", "ordered members"),
        ("foreign-member", "cross run identity"),
        ("duplicate-member", "duplicate members"),
        ("quarantine-hash", "divergent quarantine"),
        ("consume-error", "forbids error_hash"),
        ("transform-shape", "single or multi shape"),
        ("single-cardinality", "exactly one output"),
        ("transform-action", "illegal member action"),
        ("transform-parent", "first consumed"),
        ("all-quarantined", "first consumed"),
        ("empty-transform-shape", "empty shape"),
        ("empty-transform-action", "illegal member action"),
        ("passthrough-cardinality", "one output per member"),
        ("passthrough-action", "illegal member action"),
        ("empty-passthrough-parent", "empty shape"),
        ("empty-passthrough-action", "illegal member action"),
        ("output-hash", "output hash disagrees"),
        ("missing-state", "wrong-node state"),
        ("wrong-state-node", "wrong-node state"),
        ("missing-batch", "wrong-node batch"),
        ("reversed-members", "exact ordered batch membership"),
    ],
)
def test_inconsistent_verdict_cannot_complete_a_batch(recorder_setup: RecorderSetup, defect: str, message: str) -> None:
    setup, verdict = _prepare(recorder_setup)
    first, second = verdict.members
    dropped = tuple(replace(member, action=AggregationMemberAction.DROP_FILTERED) for member in verdict.members)
    if defect == "nominal-mode":
        verdict = replace(verdict, mode=cast(OutputMode, "transform"))
    elif defect == "empty-members":
        verdict = replace(verdict, members=())
    elif defect == "foreign-member":
        verdict = replace(verdict, members=(replace(first, member_ref=TokenRef(token_id="tok-0", run_id="other")), second))
    elif defect == "duplicate-member":
        verdict = replace(verdict, members=(first, first))
    elif defect == "quarantine-hash":
        verdict = replace(verdict, members=(replace(first, action=AggregationMemberAction.QUARANTINE, error_hash="wrong"), second))
    elif defect == "consume-error":
        verdict = replace(verdict, members=(replace(first, error_hash="wrong"), second))
    elif defect == "transform-shape":
        verdict = replace(verdict, shape="empty")
    elif defect == "single-cardinality":
        verdict = replace(verdict, rows=verdict.rows * 2)
    elif defect == "transform-action":
        verdict = replace(verdict, members=dropped)
    elif defect == "transform-parent":
        verdict = replace(verdict, parent="tok-1")
    elif defect == "all-quarantined":
        from hashlib import sha256

        verdict = replace(
            verdict,
            members=tuple(
                replace(
                    member,
                    action=AggregationMemberAction.QUARANTINE,
                    error_hash=sha256(f"quarantined_in_batch:batch-1:{ordinal}".encode()).hexdigest()[:16],
                )
                for ordinal, member in enumerate(verdict.members)
            ),
        )
    elif defect == "empty-transform-shape":
        verdict = replace(verdict, rows=(), members=dropped, parent=None)
    elif defect == "empty-transform-action":
        verdict = replace(verdict, rows=(), shape="empty", parent=None)
    elif defect == "passthrough-cardinality":
        verdict = replace(verdict, mode=OutputMode.PASSTHROUGH, shape="multi", parent=None)
    elif defect == "passthrough-action":
        verdict = replace(verdict, mode=OutputMode.PASSTHROUGH, rows=verdict.rows * 2, shape="multi", parent=None)
    elif defect == "empty-passthrough-parent":
        verdict = replace(verdict, mode=OutputMode.PASSTHROUGH, rows=(), shape="empty", members=dropped)
    elif defect == "empty-passthrough-action":
        verdict = replace(verdict, mode=OutputMode.PASSTHROUGH, rows=(), shape="empty", parent=None)
    elif defect == "output-hash":
        verdict = replace(verdict, output_hash=stable_hash({"total": 4}))
    elif defect == "missing-state":
        verdict = replace(verdict, state_id="missing")
    elif defect == "wrong-state-node":
        with setup.db.write_connection() as conn:
            conn.execute(update(node_states_table).values(node_id=setup.source_node_id))
    elif defect == "missing-batch":
        verdict = replace(verdict, batch_id="missing")
    elif defect == "reversed-members":
        verdict = replace(verdict, members=(second, first), parent="tok-1")
    else:
        raise AssertionError(defect)
    before = _durable(setup)
    try:
        with pytest.raises(AuditIntegrityError, match=message):
            _complete(setup, verdict)
    finally:
        assert _durable(setup) == before


@pytest.mark.parametrize("part", ["header", "outputs", "members", "completion", "trigger"])
def test_exact_retry_refuses_corrupted_durable_receipt(recorder_setup: RecorderSetup, part: str) -> None:
    setup, verdict = _prepare(recorder_setup)
    receipt = _complete(setup, verdict)
    assert _complete(setup, verdict) == receipt
    with setup.db.write_connection() as conn:
        if part == "header":
            conn.execute(update(aggregation_results_table).values(output_hash=stable_hash({"changed": True})))
        elif part == "outputs":
            conn.execute(delete(aggregation_result_outputs_table))
        elif part == "members":
            conn.execute(delete(aggregation_result_members_table).where(aggregation_result_members_table.c.ordinal == 1))
        elif part == "completion":
            conn.execute(update(node_states_table).values(duration_ms=2.0))
        else:
            conn.execute(update(batches_table).values(trigger_reason="changed"))
    before = _durable(setup)
    with pytest.raises(AuditIntegrityError, match="retry diverges"):
        _complete(setup, verdict)
    assert _durable(setup) == before


@pytest.mark.parametrize("mode", [OutputMode.TRANSFORM, OutputMode.PASSTHROUGH])
def test_empty_drop_verdict_has_no_outputs_and_exact_retry(recorder_setup: RecorderSetup, mode: OutputMode) -> None:
    setup, verdict = _prepare(recorder_setup)
    verdict = replace(
        verdict,
        mode=mode,
        rows=(),
        shape="empty",
        parent=None,
        output_hash=stable_hash([]),
        members=tuple(replace(member, action=AggregationMemberAction.DROP_FILTERED) for member in verdict.members),
    )
    receipt = _complete(setup, verdict)
    after = _durable(setup)
    assert _complete(setup, verdict) == receipt
    assert _durable(setup) == after
    with setup.db.read_only_connection() as conn:
        assert conn.execute(select(aggregation_result_outputs_table)).all() == []
