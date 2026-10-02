"""A reclaimed scheduler claim reopens a config gate at a new audit attempt."""

from sqlalchemy import select

from elspeth.contracts import TokenInfo
from elspeth.contracts.enums import NodeStateStatus, NodeType, TerminalPath
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.core.canonical import stable_hash
from elspeth.core.config import GateSettings
from elspeth.core.dag.graph import RouteDestination
from elspeth.core.landscape.schema import node_states_table, scheduler_events_table, token_work_items_table
from elspeth.engine.processor import DAGTraversalContext, RowProcessor
from elspeth.engine.spans import SpanFactory
from tests.fixtures.landscape import expire_lease, leader_coordination_token, make_recorder_with_run, register_test_node


def test_reclaimed_gate_claim_persists_second_attempt() -> None:
    """Real claim and recovery verbs feed a gate whose first audit write survived the lost lease."""
    setup = make_recorder_with_run(run_id="gate-claim-recovery", source_node_id="source", leader_worker_id="leader")
    gate_id = NodeID(register_test_node(setup.data_flow, setup.run_id, "gate", node_type=NodeType.GATE, plugin_name="gate"))
    gate = GateSettings(name="gate", input="inbound", condition="True", routes={"true": "discard", "false": "discard"})
    leader = leader_coordination_token(setup.factory, setup.run_id)
    follower = setup.factory.run_coordination.admit_follower(
        run_id=setup.run_id, worker_id="follower", config_hash=stable_hash({}), window_seconds=80
    )
    contract = SchemaContract(mode="OBSERVED", fields=(), locked=True)
    row_data = PipelineRow({"value": 1}, contract)
    row, stored_token = setup.data_flow.create_row_with_token(
        source_node_id=setup.source_node_id,
        row_index=0,
        data=row_data.to_dict(),
        source_row_index=0,
        ingest_sequence=0,
        coordination_token=leader,
    )
    token = TokenInfo(row_id=row.row_id, token_id=stored_token.token_id, row_data=row_data)
    scheduler = setup.factory.scheduler
    scheduled = scheduler.enqueue_ready(
        member_token=leader.membership,
        token_id=token.token_id,
        row_id=token.row_id,
        node_id=gate_id,
        step_index=1,
        ingest_sequence=0,
        row_payload_json=scheduler.serialize_row_payload(row_data),
    )
    first_claim = scheduler.claim_ready(member_token=follower, lease_owner=follower.worker_id, lease_seconds=30)
    assert first_claim is not None and first_claim.work_item_id == scheduled.work_item_id and first_claim.attempt == 1

    processor = RowProcessor(
        execution=setup.execution,
        data_flow=setup.data_flow,
        span_factory=SpanFactory(),
        run_id=setup.run_id,
        source_node_id=NodeID(setup.source_node_id),
        source_on_success="default",
        route_resolution_map={(gate_id, "true"): RouteDestination.discard()},
        traversal=DAGTraversalContext(
            node_step_map={NodeID(setup.source_node_id): 0, gate_id: 1},
            node_to_plugin={gate_id: gate},
            node_to_next={gate_id: None},
            coalesce_node_map={},
        ),
        scheduler=scheduler,
        scheduler_lease_owner=leader.membership.worker_id,
        coordination_token=leader,
    )
    # The first owner finished the gate write, then left before disposing its
    # scheduler claim. Only that gate node_state survives from the attempt.
    processor._gate_executor.execute_config_gate(
        gate, gate_id, token, PluginContext(run_id=setup.run_id, config={}, member_token=follower), attempt_offset=0
    )
    expire_lease(setup.db.engine, first_claim.work_item_id)
    setup.factory.run_coordination.depart_worker(member_token=follower)
    assert scheduler.recover_expired_leases(coordination_token=leader) == 1

    results = processor._drain_scheduler_claims(
        ctx=PluginContext(run_id=setup.run_id, config={}, coordination_token=leader),
        pending_items={},
        recover_pending_sinks=False,
    )
    assert len(results) == 1 and results[0].path is TerminalPath.GATE_DISCARDED

    with setup.db.engine.connect() as conn:
        attempts = conn.execute(
            select(node_states_table.c.attempt, node_states_table.c.status, node_states_table.c.resume_checkpoint_id)
            .where(node_states_table.c.token_id == token.token_id, node_states_table.c.node_id == gate_id)
            .order_by(node_states_table.c.attempt)
        ).all()
        item = conn.execute(
            select(token_work_items_table.c.attempt, token_work_items_table.c.status).where(
                token_work_items_table.c.token_id == token.token_id
            )
        ).one()
        recoveries = conn.execute(
            select(scheduler_events_table.c.from_attempt, scheduler_events_table.c.to_attempt).where(
                scheduler_events_table.c.token_id == token.token_id,
                scheduler_events_table.c.event_type == SchedulerEventType.RECOVER_EXPIRED_LEASE.value,
            )
        ).all()

    assert [attempt.attempt for attempt in attempts] == [0, 1]
    assert all(attempt.status == NodeStateStatus.COMPLETED.value for attempt in attempts)
    assert all(attempt.resume_checkpoint_id is None for attempt in attempts)
    assert item.attempt == 2 and item.status == TokenWorkStatus.TERMINAL.value
    assert [(event.from_attempt, event.to_attempt) for event in recoveries] == [(1, 2)]
