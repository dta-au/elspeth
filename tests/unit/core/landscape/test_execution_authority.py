"""Execution writes bind current authority to their actual persisted subjects."""

from dataclasses import dataclass, replace

import pytest
from sqlalchemy import func, select

from elspeth.contracts import BatchStatus, CallStatus, CallType, NodeStateStatus, NodeType, RoutingMode, RoutingSpec
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.errors import AuditIntegrityError, RunLeadershipLostError, RunMembershipLostError, SchedulerLeaseLostError
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import calls_table, node_states_table, operations_table, token_work_items_table
from tests.fixtures.landscape import RecorderSetup, leader_coordination_token, make_recorder_with_run, register_test_node
from tests.fixtures.stores import MockPayloadStore


@dataclass
class _ExecutionSetup:
    recorder: RecorderSetup
    member: WorkerMembershipToken
    item: TokenWorkItem


def _setup(*, payload_store: MockPayloadStore | None = None) -> _ExecutionSetup:
    recorder = make_recorder_with_run(leader_worker_id="leader", payload_store=payload_store)
    register_test_node(recorder.data_flow, recorder.run_id, "transform")
    register_test_node(recorder.data_flow, recorder.run_id, "aggregate", node_type=NodeType.AGGREGATION)
    run = recorder.run_lifecycle.get_run(recorder.run_id)
    assert run is not None
    member = recorder.factory.run_coordination.admit_follower(
        run_id=recorder.run_id,
        worker_id="follower",
        config_hash=run.config_hash,
        window_seconds=300,
    )
    row, token = recorder.data_flow.create_row_with_token(
        recorder.source_node_id,
        0,
        {"value": 1},
        source_row_index=0,
        ingest_sequence=0,
        coordination_token=recorder.coordination_token,
    )
    item = recorder.factory.scheduler.enqueue_ready_claimed(
        member_token=member,
        token_id=token.token_id,
        row_id=row.row_id,
        node_id="transform",
        step_index=1,
        ingest_sequence=0,
        row_payload_json='{"value":1}',
        lease_owner=member.worker_id,
        lease_seconds=300,
    )
    return _ExecutionSetup(recorder, member, item)


def _state(setup: _ExecutionSetup) -> str:
    return setup.recorder.execution.begin_node_state(
        setup.item.token_id,
        "transform",
        1,
        {"value": 1},
        member_token=setup.member,
    ).state_id


def _stale(token: CoordinationToken) -> CoordinationToken:
    # The malformed epoch is the rejection subject, not authority for setup.
    return replace(token, leader_epoch=token.leader_epoch + 1)


def test_follower_records_state_and_call_without_leader_seat() -> None:
    setup = _setup()
    state_id = _state(setup)
    repo = setup.recorder.execution
    index = repo.allocate_call_index(state_id, member_token=setup.member, work_item=setup.item)
    call = repo.record_call(
        state_id,
        index,
        CallType.HTTP,
        CallStatus.SUCCESS,
        RawCallPayload({"request": 1}),
        RawCallPayload({"response": 2}),
        member_token=setup.member,
        work_item=setup.item,
    )
    assert call.request_ref is not None
    assert call.response_ref is not None
    completed = repo.complete_node_state(
        state_id,
        NodeStateStatus.COMPLETED,
        output_data={"value": 1},
        duration_ms=1,
        member_token=setup.member,
    )
    assert completed.status is NodeStateStatus.COMPLETED
    assert setup.recorder.coordination_token.worker_id != setup.member.worker_id


def test_departed_member_cannot_begin_or_complete_state() -> None:
    setup = _setup()
    state_id = _state(setup)
    setup.recorder.factory.run_coordination.depart_worker(member_token=setup.member)
    with pytest.raises(RunMembershipLostError):
        setup.recorder.execution.begin_node_state(
            setup.item.token_id,
            "transform",
            2,
            {},
            member_token=setup.member,
        )
    with pytest.raises(RunMembershipLostError):
        setup.recorder.execution.complete_node_state(
            state_id,
            NodeStateStatus.COMPLETED,
            output_data={},
            duration_ms=0,
            member_token=setup.member,
        )
    assert setup.recorder.execution.get_node_state(state_id).status is NodeStateStatus.OPEN
    with setup.recorder.db.read_only_connection() as conn:
        assert conn.execute(select(func.count()).select_from(node_states_table)).scalar_one() == 1


@pytest.mark.parametrize("verb", ["allocate", "record"])
def test_stale_item_cannot_allocate_or_record_calls(verb: str) -> None:
    setup = _setup()
    state_id = _state(setup)
    # Recovery transferred the durable attempt while this worker retained its
    # old claimed value; its membership is still active.
    with setup.recorder.db.write_connection() as conn:
        conn.execute(
            token_work_items_table.update()
            .where(
                token_work_items_table.c.work_item_id == setup.item.work_item_id,
            )
            .values(lease_owner="leader")
        )
    with pytest.raises(SchedulerLeaseLostError):
        if verb == "allocate":
            setup.recorder.execution.allocate_call_index(state_id, member_token=setup.member, work_item=setup.item)
        else:
            setup.recorder.execution.record_call(
                state_id,
                0,
                CallType.HTTP,
                CallStatus.SUCCESS,
                RawCallPayload({}),
                member_token=setup.member,
                work_item=setup.item,
            )
    with setup.recorder.db.read_only_connection() as conn:
        assert conn.execute(select(func.count()).select_from(calls_table)).scalar_one() == 0


def test_item_cannot_authorize_a_different_tokens_state() -> None:
    setup = _setup()
    row, token = setup.recorder.data_flow.create_row_with_token(
        setup.recorder.source_node_id,
        1,
        {"value": 2},
        source_row_index=1,
        ingest_sequence=1,
        coordination_token=setup.recorder.coordination_token,
    )
    assert row.row_id != setup.item.row_id
    state = setup.recorder.execution.begin_node_state(token.token_id, "transform", 1, {}, member_token=setup.member)
    with pytest.raises(AuditIntegrityError, match="claimed work item"):
        setup.recorder.execution.allocate_call_index(state.state_id, member_token=setup.member, work_item=setup.item)


@pytest.mark.parametrize("verb", ["begin_operation", "record_completed_node_state", "begin_node_states_many", "create_batch"])
def test_stale_leader_cannot_create_execution_records(verb: str) -> None:
    setup = _setup()
    repo = setup.recorder.execution
    stale = _stale(setup.recorder.coordination_token)
    with pytest.raises(RunLeadershipLostError):
        if verb == "begin_operation":
            repo.begin_operation(setup.recorder.source_node_id, "source_load", coordination_token=stale)
        elif verb == "record_completed_node_state":
            repo.record_completed_node_state(
                setup.item.token_id,
                "transform",
                1,
                {},
                {},
                0,
                coordination_token=stale,
            )
        elif verb == "begin_node_states_many":
            repo.begin_node_states_many([(setup.item.token_id, "transform", 1, {})], coordination_token=stale)
        else:
            repo.create_batch("aggregate", coordination_token=stale)


@pytest.mark.parametrize("verb", ["complete", "allocate", "record"])
def test_stale_leader_cannot_mutate_operation(verb: str) -> None:
    setup = _setup()
    repo = setup.recorder.execution
    operation = repo.begin_operation(setup.recorder.source_node_id, "source_load", coordination_token=setup.recorder.coordination_token)
    stale = _stale(setup.recorder.coordination_token)
    with pytest.raises(RunLeadershipLostError):
        if verb == "complete":
            repo.complete_operation(operation.operation_id, "completed", duration_ms=1, coordination_token=stale)
        elif verb == "allocate":
            repo.allocate_operation_call_index(operation.operation_id, coordination_token=stale)
        else:
            repo.record_operation_call(
                operation.operation_id,
                CallType.HTTP,
                CallStatus.SUCCESS,
                RawCallPayload({}),
                coordination_token=stale,
            )
    assert repo.get_operation(operation.operation_id).status == "open"


@pytest.mark.parametrize("verb", ["update", "complete", "retry"])
def test_stale_leader_cannot_mutate_batch(verb: str) -> None:
    setup = _setup()
    repo = setup.recorder.execution
    batch = repo.create_batch("aggregate", coordination_token=setup.recorder.coordination_token)
    stale = _stale(setup.recorder.coordination_token)
    with pytest.raises(RunLeadershipLostError):
        if verb == "update":
            repo.update_batch_status(batch.batch_id, BatchStatus.EXECUTING, coordination_token=stale)
        elif verb == "complete":
            repo.complete_batch(batch.batch_id, BatchStatus.FAILED, coordination_token=stale)
        else:
            repo.retry_batch(batch.batch_id, coordination_token=stale)
    assert repo.get_batch(batch.batch_id).status is BatchStatus.DRAFT


def test_current_leader_cannot_complete_foreign_operation_or_batch() -> None:
    setup = _setup()
    other = setup.recorder.run_lifecycle.begin_run(config={}, canonical_version="v1")
    repo = setup.recorder.execution
    operation = repo.begin_operation(setup.recorder.source_node_id, "source_load", coordination_token=setup.recorder.coordination_token)
    foreign = leader_coordination_token(setup.recorder.factory, other.run_id)
    with pytest.raises(AuditIntegrityError, match="foreign run"):
        repo.complete_operation(operation.operation_id, "completed", duration_ms=1, coordination_token=foreign)
    assert repo.get_operation(operation.operation_id).status == "open"
    batch = repo.create_batch("aggregate", coordination_token=setup.recorder.coordination_token)
    with pytest.raises(AuditIntegrityError, match="foreign run"):
        repo.complete_batch(batch.batch_id, BatchStatus.FAILED, coordination_token=foreign)
    assert repo.get_batch(batch.batch_id).status is BatchStatus.DRAFT


def test_operation_call_rechecks_authority_after_payload_storage() -> None:
    setup = _setup()
    repo = setup.recorder.execution
    operation = repo.begin_operation(setup.recorder.source_node_id, "source_load", coordination_token=setup.recorder.coordination_token)

    class DeposingStore(MockPayloadStore):
        def store(self, content: bytes) -> str:
            result = super().store(content)
            setup.recorder.factory.run_coordination.release_seat(token=setup.recorder.coordination_token)
            return result

    repo.calls._payload_store = DeposingStore()
    with pytest.raises(RunLeadershipLostError):
        repo.record_operation_call(
            operation.operation_id,
            CallType.HTTP,
            CallStatus.SUCCESS,
            RawCallPayload({"request": 1}),
            coordination_token=setup.recorder.coordination_token,
        )
    with setup.recorder.db.read_only_connection() as conn:
        call = conn.execute(select(calls_table)).one()
        assert call.request_hash is not None
        assert call.request_ref is None
        assert conn.execute(select(operations_table.c.status)).scalar_one() == "open"


@pytest.mark.parametrize("plural", [False, True])
def test_follower_can_record_routing_decisions(plural: bool) -> None:
    setup = _setup()
    state_id = _state(setup)
    edge = setup.recorder.data_flow.register_edge(
        "transform",
        "aggregate",
        "next",
        RoutingMode.MOVE,
        coordination_token=leader_coordination_token(setup.recorder.factory, setup.recorder.run_id),
    )
    if plural:
        events = setup.recorder.execution.record_routing_events(
            state_id,
            [RoutingSpec(edge_id=edge.edge_id, mode=RoutingMode.MOVE)],
            member_token=setup.member,
            work_item=setup.item,
        )
        assert events[0].edge_id == edge.edge_id
    else:
        event = setup.recorder.execution.record_routing_event(
            state_id,
            edge.edge_id,
            RoutingMode.MOVE,
            member_token=setup.member,
        )
        assert event.edge_id == edge.edge_id


def test_empty_bulk_calls_still_require_current_authority() -> None:
    setup = _setup()
    with pytest.raises(RunLeadershipLostError):
        setup.recorder.execution.begin_node_states_many([], coordination_token=_stale(setup.recorder.coordination_token))
    setup.recorder.factory.run_coordination.depart_worker(member_token=setup.member)
    with pytest.raises(RunMembershipLostError):
        setup.recorder.execution.record_routing_events("unused", [], member_token=setup.member, work_item=setup.item)


def test_live_bulk_completion_helper_preserves_terminal_state_immutability() -> None:
    setup = _setup()
    repo = setup.recorder.execution
    state_id = _state(setup)
    second = repo.begin_node_state(setup.item.token_id, "aggregate", 2, {}, member_token=setup.member)
    repo.complete_node_state(state_id, NodeStateStatus.COMPLETED, output_data={"value": 1}, duration_ms=1, member_token=setup.member)
    with (
        pytest.raises(LandscapeRecordError, match="already terminal"),
        fenced_leader_transaction(
            setup.recorder.db.engine,
            token=setup.recorder.coordination_token,
            window_seconds=300,
            verb="test_bulk_completion",
        ) as conn,
    ):
        repo.node_states.complete_node_states_completed_many(
            [(second.state_id, {"value": 2}, 1), (state_id, {"value": 2}, 1)],
            conn=conn,
        )
    assert repo.get_node_state(second.state_id).status is NodeStateStatus.OPEN
    assert repo.get_node_state(state_id).output_hash == stable_hash({"value": 1})
