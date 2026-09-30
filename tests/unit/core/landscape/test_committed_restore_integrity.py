"""Committed recovery receipts are admitted only as a coherent durable image."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from elspeth.contracts import AggregationResultMember, BatchStatus, NodeType, OutputMode, TriggerType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import AggregationMemberAction, TerminalPath
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
    coalesce_effect_members_table,
    coalesce_effects_table,
    node_states_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
)
from tests.fixtures.landscape import RecorderSetup, leader_coordination_token, make_recorder_with_run, register_test_node
from tests.unit.core.landscape.test_aggregation_result_receipt import _add_batch_member
from tests.unit.core.landscape.test_coalesce_effects import _COALESCE_NODE_ID, _materialize, _setup


def _snapshot(setup: RecorderSetup) -> tuple:
    with setup.db.connection() as conn:
        return tuple(
            tuple(conn.execute(select(table)).all())
            for table in (
                coalesce_effects_table,
                coalesce_effect_members_table,
                tokens_table,
                node_states_table,
                token_outcomes_table,
                token_work_items_table,
                batches_table,
                batch_members_table,
                aggregation_results_table,
                aggregation_result_members_table,
                aggregation_result_outputs_table,
            )
        )


def _corrupt(setup: RecorderSetup, statement: str) -> None:
    """Model damaged persisted evidence; the public reader must still refuse it."""
    connection = setup.db.engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys = OFF")
        cursor.execute("PRAGMA ignore_check_constraints = ON")
        cursor.execute(statement)
        connection.commit()
        cursor.execute("PRAGMA ignore_check_constraints = OFF")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()
    finally:
        connection.close()


@pytest.mark.parametrize(
    "statement, message",
    [
        ("DELETE FROM coalesce_effect_members WHERE ordinal = 1", "identity hashes"),
        ("UPDATE coalesce_effect_members SET ordinal = 3 WHERE ordinal = 1", "ordered membership"),
        ("UPDATE coalesce_effect_members SET parent_state_id = '' WHERE ordinal = 1", "ordered membership"),
        (
            "DELETE FROM token_outcomes WHERE token_id = (SELECT parent_token_id FROM coalesce_effect_members WHERE ordinal = 1)",
            "terminal parent outcomes",
        ),
        ("UPDATE token_outcomes SET path = 'discarded', outcome = 'failure'", "divergent parent outcomes"),
        ("UPDATE tokens SET token_data_ref = 'changed' WHERE join_group_id IS NOT NULL", "result-token identity"),
        ("UPDATE tokens SET step_in_pipeline = 99 WHERE join_group_id IS NOT NULL", "identity hashes"),
        ("UPDATE coalesce_effects SET parent_set_hash = 'changed'", "identity hashes"),
        ("UPDATE coalesce_effects SET effect_hash = 'changed'", "identity hashes"),
    ],
)
def test_coalesce_restore_refuses_corrupt_committed_identity_without_writes(statement: str, message: str) -> None:
    setup, row, refs, completions = _setup()
    merged = _materialize(setup, row, refs, completions)
    setup.data_flow.finalize_coalesce_effect(
        merged=merged,
        parent_completions=completions,
        coordination_token=leader_coordination_token(setup.factory, setup.run_id),
    )
    read = setup.factory.barrier_restore
    arguments = {
        "coalesce_node_id": _COALESCE_NODE_ID,
        "coalesce_name": "merge",
        "group_id": "coalesce-effects-fork-grp",
        "blocked_token_ids": tuple(ref.token_id for ref in refs),
    }
    assert read.get_committed_coalesce_residual(setup.run_id, **arguments).result_token_id == merged.token_id
    _corrupt(setup, statement)
    before = _snapshot(setup)
    with pytest.raises(AuditIntegrityError, match=message):
        read.get_committed_coalesce_residual(setup.run_id, **arguments)
    assert _snapshot(setup) == before
    setup.db.close()


def test_coalesce_restore_distinguishes_absent_partial_and_already_continued_groups() -> None:
    setup, row, refs, completions = _setup()
    merged = _materialize(setup, row, refs, completions)
    setup.data_flow.finalize_coalesce_effect(
        merged=merged,
        parent_completions=completions,
        coordination_token=leader_coordination_token(setup.factory, setup.run_id),
    )
    read = setup.factory.barrier_restore
    arguments = {"coalesce_node_id": _COALESCE_NODE_ID, "coalesce_name": "merge", "group_id": "coalesce-effects-fork-grp"}
    assert read.get_committed_coalesce_residual(setup.run_id, **arguments, blocked_token_ids=()) is None
    assert read.get_committed_coalesce_residual(setup.run_id, **arguments, blocked_token_ids=("unrelated",)) is None
    with pytest.raises(AuditIntegrityError, match="partial BLOCKED"):
        read.get_committed_coalesce_residual(setup.run_id, **arguments, blocked_token_ids=(refs[0].token_id,))
    setup.factory.scheduler.enqueue_ready_claimed(
        member_token=setup.coordination_token.membership,
        token_id=merged.token_id,
        row_id=row.row_id,
        node_id=_COALESCE_NODE_ID,
        step_index=5,
        ingest_sequence=0,
        row_payload_json='{"merged":true}',
        lease_owner=setup.coordination_token.worker_id,
        lease_seconds=60,
    )
    before = _snapshot(setup)
    with pytest.raises(AuditIntegrityError, match="continuation evidence"):
        read.get_committed_coalesce_residual(setup.run_id, **arguments, blocked_token_ids=tuple(ref.token_id for ref in refs))
    assert _snapshot(setup) == before
    setup.db.close()


def _aggregation() -> RecorderSetup:
    setup = make_recorder_with_run()
    register_test_node(setup.data_flow, setup.run_id, "agg", node_type=NodeType.AGGREGATION, plugin_name="aggregator")
    for ordinal in range(2):
        setup.data_flow.create_row_with_token(
            setup.source_node_id,
            ordinal,
            {"value": ordinal},
            token_id=f"tok-{ordinal}",
            source_row_index=ordinal,
            ingest_sequence=ordinal,
            coordination_token=setup.coordination_token,
        )
    state = setup.execution.begin_node_state(
        "tok-0",
        "agg",
        1,
        {"value": 0},
        member_token=setup.coordination_token.membership,
    )
    setup.execution.create_batch("agg", batch_id="batch", coordination_token=setup.coordination_token)
    for ordinal in range(2):
        _add_batch_member(setup, "batch", f"tok-{ordinal}", ordinal)
    with setup.db.write_connection() as conn:
        for ordinal in range(2):
            record_buffered_outcome_guarded(
                conn,
                run_id=setup.run_id,
                token_id=f"tok-{ordinal}",
                batch_id="batch",
                recorded_at=datetime.now(UTC),
            )
    setup.execution.update_batch_status(
        "batch",
        BatchStatus.EXECUTING,
        state_id=state.state_id,
        coordination_token=setup.coordination_token,
    )
    output = PipelineRow({"total": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
    setup.execution.complete_aggregation_result(
        batch_id="batch",
        aggregation_node_id="agg",
        state_id=state.state_id,
        trigger_type=TriggerType.END_OF_SOURCE,
        output_mode=OutputMode.TRANSFORM,
        output_rows=(output,),
        output_shape="single",
        output_hash=stable_hash(output.to_dict()),
        members=tuple(
            AggregationResultMember(
                member_ref=TokenRef(token_id=f"tok-{ordinal}", run_id=setup.run_id),
                action=AggregationMemberAction.CONSUME_BATCH,
                error_hash=None,
            )
            for ordinal in range(2)
        ),
        expansion_parent_token_id="tok-0",
        duration_ms=1.0,
        success_reason=None,
        context_after=AggregationFlushContext(
            trigger_type=TriggerType.END_OF_SOURCE.value,
            buffer_size=2,
            batch_id="batch",
            flush_index=1,
            rows_seen_total=2,
            row_start=1,
            row_end=2,
            is_end_of_source=True,
        ),
        coordination_token=setup.coordination_token,
    )
    return setup


@pytest.mark.parametrize(
    "statement,message",
    [
        ("DELETE FROM node_states WHERE node_id = 'agg'", "missing its batch or node state"),
        ("UPDATE batches SET expansion_group_id = 'claimed'", "expansion claim"),
        ("UPDATE aggregation_results SET output_mode = 'bogus'", "unknown output mode"),
        ("UPDATE node_states SET output_hash = 'changed' WHERE node_id = 'agg'", "completion identity"),
        ("UPDATE aggregation_results SET output_shape = 'empty'", "completion identity"),
        ("UPDATE batch_members SET ordinal = 3 WHERE ordinal = 1", "invalid batch membership"),
        ("DELETE FROM aggregation_result_members WHERE ordinal = 1", "ordered member actions"),
        ("UPDATE aggregation_result_members SET action = 'bogus' WHERE ordinal = 1", "unknown member action"),
        ("UPDATE aggregation_result_members SET action = 'quarantine', error_hash = 'changed' WHERE ordinal = 1", "quarantine action"),
        ("UPDATE aggregation_result_members SET error_hash = 'changed' WHERE ordinal = 1", "non-quarantine action"),
        ("UPDATE aggregation_result_members SET action = 'drop_filtered' WHERE ordinal = 1", "illegal transform member actions"),
        ("UPDATE aggregation_results SET expansion_parent_token_id = 'tok-1'", "expansion parent"),
        ("DELETE FROM token_outcomes WHERE token_id = 'tok-1'", "live BUFFERED outcome"),
        ("DELETE FROM aggregation_result_outputs", "output references"),
        ("UPDATE aggregation_result_outputs SET ordinal = 3", "output references"),
        ("UPDATE aggregation_result_outputs SET token_data_ref = 'changed'", "output references"),
    ],
)
def test_aggregation_output_restore_refuses_damaged_receipt_without_writes(statement: str, message: str) -> None:
    setup = _aggregation()
    read = setup.factory.barrier_restore
    arguments = {"aggregation_node_id": "agg", "blocked_token_ids": ("tok-0", "tok-1")}
    valid = read.list_committed_aggregation_output_receipts(setup.run_id, **arguments)
    assert len(valid) == 1
    assert valid[0].member_token_ids == ("tok-0", "tok-1")
    _corrupt(setup, statement)
    before = _snapshot(setup)
    with pytest.raises(AuditIntegrityError, match=message):
        read.list_committed_aggregation_output_receipts(setup.run_id, **arguments)
    assert _snapshot(setup) == before
    setup.db.close()


def test_restore_distinguishes_absence_and_partial_blocked_receipts() -> None:
    setup = _aggregation()
    read = setup.factory.barrier_restore
    assert read.list_committed_aggregation_output_receipts(setup.run_id, aggregation_node_id="agg", blocked_token_ids=()) == ()
    assert read.list_committed_aggregation_residuals(setup.run_id, aggregation_node_id="agg", blocked_token_ids=()) == ()
    assert read.list_committed_aggregation_output_receipts("other-run", aggregation_node_id="agg", blocked_token_ids=("tok-0",)) == ()
    before = _snapshot(setup)
    with pytest.raises(AuditIntegrityError, match="BLOCKED member set"):
        read.list_committed_aggregation_output_receipts(setup.run_id, aggregation_node_id="agg", blocked_token_ids=("tok-0",))
    assert _snapshot(setup) == before
    setup.db.close()


@pytest.mark.parametrize(
    "statement,message",
    [
        ("UPDATE batches SET aggregation_node_id = 'source-0'", "belongs to another node"),
        ("UPDATE node_states SET status = 'open' WHERE node_id = 'agg'", "completed node/result expansion receipt"),
        ("UPDATE batch_members SET ordinal = 3 WHERE ordinal = 1", "non-contiguous membership"),
        ("DELETE FROM token_outcomes WHERE token_id = 'tok-1' AND completed = 1", "one terminal outcome"),
        (
            "UPDATE token_outcomes SET outcome = 'failure', path = 'discarded' WHERE token_id = 'tok-1' AND completed = 1",
            "divergent terminal outcome",
        ),
        ("DELETE FROM token_parents", "exact ordered child set"),
        (
            "UPDATE token_parents SET parent_token_id = (SELECT token_id FROM tokens WHERE token_id NOT IN ('tok-0', 'tok-1') LIMIT 1)",
            "divergent child parentage",
        ),
        ("UPDATE tokens SET token_data_ref = NULL WHERE token_id NOT IN ('tok-0', 'tok-1')", "incomplete child receipt"),
    ],
)
def test_aggregation_claim_restore_refuses_damaged_children_without_writes(statement: str, message: str) -> None:
    setup = _aggregation()
    with setup.db.connection() as conn:
        parent_row_id = conn.execute(select(tokens_table.c.row_id).where(tokens_table.c.token_id == "tok-0")).scalar_one()
    children, _group = setup.data_flow.expand_token(
        parent_ref=TokenRef(token_id="tok-0", run_id=setup.run_id),
        row_id=parent_row_id,
        child_payloads=[{"total": 1}],
        output_contract=SchemaContract(mode="OBSERVED", fields=(), locked=True),
        parent_path=TerminalPath.BATCH_CONSUMED,
        parent_batch_id="batch",
        step_in_pipeline=1,
        member_token=setup.coordination_token.membership,
    )
    read = setup.factory.barrier_restore
    arguments = {"aggregation_node_id": "agg", "blocked_token_ids": ("tok-0", "tok-1")}
    valid = read.list_committed_aggregation_residuals(setup.run_id, **arguments)
    assert len(valid) == 1
    assert valid[0].children[0].token_id == children[0].token_id
    _corrupt(setup, statement)
    before = _snapshot(setup)
    with pytest.raises(AuditIntegrityError, match=message):
        read.list_committed_aggregation_residuals(setup.run_id, **arguments)
    assert _snapshot(setup) == before
    setup.db.close()


def test_aggregation_claim_cannot_release_partial_or_already_continued_children() -> None:
    setup = _aggregation()
    with setup.db.connection() as conn:
        parent_row_id = conn.execute(select(tokens_table.c.row_id).where(tokens_table.c.token_id == "tok-0")).scalar_one()
    children, _group = setup.data_flow.expand_token(
        parent_ref=TokenRef(token_id="tok-0", run_id=setup.run_id),
        row_id=parent_row_id,
        child_payloads=[{"total": 1}],
        output_contract=SchemaContract(mode="OBSERVED", fields=(), locked=True),
        parent_path=TerminalPath.BATCH_CONSUMED,
        parent_batch_id="batch",
        step_in_pipeline=1,
        member_token=setup.coordination_token.membership,
    )
    read = setup.factory.barrier_restore
    with pytest.raises(AuditIntegrityError, match="exact BLOCKED member set"):
        read.list_committed_aggregation_residuals(setup.run_id, aggregation_node_id="agg", blocked_token_ids=("tok-0",))
    child = children[0]
    setup.factory.scheduler.enqueue_ready_claimed(
        member_token=setup.coordination_token.membership,
        token_id=child.token_id,
        row_id=child.row_id,
        node_id="agg",
        step_index=2,
        ingest_sequence=0,
        row_payload_json='{"total":1}',
        lease_owner=setup.coordination_token.worker_id,
        lease_seconds=60,
    )
    before = _snapshot(setup)
    with pytest.raises(AuditIntegrityError, match="continuation evidence"):
        read.list_committed_aggregation_residuals(setup.run_id, aggregation_node_id="agg", blocked_token_ids=("tok-0", "tok-1"))
    assert _snapshot(setup) == before
    setup.db.close()
