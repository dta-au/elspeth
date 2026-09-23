"""Data-flow writes require live authority and refuse before payload mutation."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any, cast

import pytest
from sqlalchemy import Select, func, select, text, update

from elspeth.contracts import CoalesceParentCompletion, NodeType, RoutingMode, Token
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError, RunLeadershipLostError, RunMembershipLostError, SchedulerLeaseLostError
from elspeth.contracts.scheduler import BarrierTerminalOutcomeSpec, TokenWorkItem
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.landscape.data_flow.outcomes import TokenOutcomeWrite, record_terminal_outcomes_guarded
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import (
    coalesce_effect_members_table,
    coalesce_effects_table,
    edges_table,
    group_records_table,
    node_states_table,
    nodes_table,
    rows_table,
    run_workers_table,
    token_lineage_frames_table,
    token_outcomes_table,
    token_parents_table,
    token_work_items_table,
    tokens_table,
    transform_errors_table,
    validation_errors_table,
)
from tests.fixtures.landscape import leader_token_for, make_factory, make_landscape_db, member_token_for

_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})
_CONTRACT = SchemaContract(mode="OBSERVED", fields=(), locked=True)
_PAYLOAD_TABLES = (
    nodes_table,
    edges_table,
    rows_table,
    tokens_table,
    token_outcomes_table,
    token_parents_table,
    token_lineage_frames_table,
    group_records_table,
    coalesce_effects_table,
    coalesce_effect_members_table,
    validation_errors_table,
    transform_errors_table,
    node_states_table,
)


@dataclass(frozen=True)
class _Harness:
    db: LandscapeDB
    factory: RecorderFactory
    leader: CoordinationToken
    member: WorkerMembershipToken
    token: Token

    @property
    def ref(self) -> TokenRef:
        return TokenRef(self.token.token_id, self.leader.run_id)


@pytest.fixture
def harness() -> Iterator[_Harness]:
    db = make_landscape_db()
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    leader = leader_token_for(db, run.run_id)
    member = member_token_for(db.engine, worker_id=leader.worker_id)
    factory.data_flow.register_node(
        "source",
        NodeType.SOURCE,
        "1.0",
        {},
        node_id="source",
        schema_config=_SCHEMA,
        coordination_token=leader,
    )
    factory.data_flow.register_node(
        "transform",
        NodeType.TRANSFORM,
        "1.0",
        {},
        node_id="transform",
        schema_config=_SCHEMA,
        coordination_token=leader,
    )
    _, token = factory.data_flow.create_row_with_token(
        "source",
        0,
        {"value": 1},
        source_row_index=0,
        ingest_sequence=0,
        coordination_token=leader,
    )
    try:
        yield _Harness(db, factory, leader, member, token)
    finally:
        db.close()


def _snapshot(h: _Harness) -> dict[str, tuple[tuple[object, ...], ...]]:
    with h.db.read_only_connection() as conn:
        return {table.name: tuple(tuple(row) for row in conn.execute(select(table))) for table in _PAYLOAD_TABLES}


def _node(h: _Harness, leader: CoordinationToken) -> None:
    h.factory.data_flow.register_node("other", NodeType.TRANSFORM, "1.0", {}, schema_config=_SCHEMA, coordination_token=leader)


def _edge(h: _Harness, leader: CoordinationToken) -> None:
    h.factory.data_flow.register_edge("source", "transform", "continue", RoutingMode.MOVE, coordination_token=leader)


def _row(h: _Harness, leader: CoordinationToken) -> None:
    h.factory.data_flow.create_row_with_token("source", 1, {"value": 2}, source_row_index=1, ingest_sequence=1, coordination_token=leader)


def _quarantine(h: _Harness, leader: CoordinationToken) -> None:
    h.factory.data_flow.create_quarantine_row_with_token(
        "source",
        1,
        {"value": 2},
        source_row_index=1,
        ingest_sequence=1,
        coordination_token=leader,
    )


def _token(h: _Harness, leader: CoordinationToken) -> None:
    h.factory.data_flow.create_token(h.token.row_id, coordination_token=leader)


def _validation(h: _Harness, leader: CoordinationToken) -> None:
    h.factory.data_flow.record_validation_error("source", {"value": "bad"}, "invalid", "observed", "discard", coordination_token=leader)


def _leader_outcome(h: _Harness, leader: CoordinationToken) -> None:
    h.factory.data_flow.record_token_outcome_leader(h.ref, None, TerminalPath.ABANDONED, coordination_token=leader)


_LEADER_WRITES = [_node, _edge, _row, _quarantine, _token, _validation, _leader_outcome]


@pytest.mark.parametrize("write", _LEADER_WRITES, ids=lambda write: write.__name__)
def test_live_leader_commits(harness: _Harness, write: Callable[[_Harness, CoordinationToken], None]) -> None:
    before = _snapshot(harness)
    write(harness, harness.leader)
    assert _snapshot(harness) != before


@pytest.mark.parametrize("write", _LEADER_WRITES, ids=lambda write: write.__name__)
def test_stale_epoch_refuses_without_payload_mutation(harness: _Harness, write: Callable[[_Harness, CoordinationToken], None]) -> None:
    before = _snapshot(harness)
    with pytest.raises(RunLeadershipLostError):
        write(harness, replace(harness.leader, leader_epoch=harness.leader.leader_epoch + 1))
    assert _snapshot(harness) == before


def _contract(h: _Harness, member: WorkerMembershipToken) -> None:
    h.factory.data_flow.update_node_output_contract("transform", _CONTRACT, member_token=member)


def _expand(h: _Harness, member: WorkerMembershipToken) -> None:
    h.factory.data_flow.expand_token(h.ref, h.token.row_id, [{"value": 2}], output_contract=_CONTRACT, member_token=member)


def _empty(h: _Harness, member: WorkerMembershipToken) -> None:
    h.factory.data_flow.record_empty_expansion(h.ref, member_token=member)


@pytest.mark.parametrize("write", [_contract, _expand, _empty], ids=lambda write: write.__name__)
def test_live_member_commits(harness: _Harness, write: Callable[[_Harness, WorkerMembershipToken], None]) -> None:
    before = _snapshot(harness)
    write(harness, harness.member)
    assert _snapshot(harness) != before


@pytest.mark.parametrize("write", [_contract, _expand, _empty], ids=lambda write: write.__name__)
def test_evicted_member_refuses_without_payload_mutation(
    harness: _Harness, write: Callable[[_Harness, WorkerMembershipToken], None]
) -> None:
    with harness.db.engine.begin() as conn:
        conn.execute(
            update(run_workers_table)
            .where(run_workers_table.c.worker_id == harness.member.worker_id)
            .values(status="evicted", evicted_at=func.current_timestamp())
        )
    before = _snapshot(harness)
    with pytest.raises(RunMembershipLostError):
        write(harness, harness.member)
    assert _snapshot(harness) == before


def _claim(h: _Harness) -> TokenWorkItem:
    return h.factory.scheduler.enqueue_ready_claimed(
        member_token=h.member,
        token_id=h.token.token_id,
        row_id=h.token.row_id,
        node_id="transform",
        step_index=1,
        ingest_sequence=0,
        row_payload_json="{}",
        lease_owner=h.member.worker_id,
        lease_seconds=60,
    )


def _fork(h: _Harness, item: TokenWorkItem) -> None:
    h.factory.data_flow.fork_token(h.ref, h.token.row_id, ["left", "right"], member_token=h.member, work_item=item)


def _item_outcome(h: _Harness, item: TokenWorkItem) -> None:
    h.factory.data_flow.record_token_outcome(h.ref, None, TerminalPath.ABANDONED, member_token=h.member, work_item=item)


def _transform_error(h: _Harness, item: TokenWorkItem) -> None:
    h.factory.data_flow.record_transform_error(
        h.ref,
        "transform",
        {"value": 1},
        {"reason": "invalid_input"},
        "discard",
        member_token=h.member,
        work_item=item,
    )


@pytest.mark.parametrize("write", [_fork, _item_outcome, _transform_error], ids=lambda write: write.__name__)
def test_live_item_claim_commits(harness: _Harness, write: Callable[[_Harness, TokenWorkItem], None]) -> None:
    item = _claim(harness)
    before = _snapshot(harness)
    write(harness, item)
    assert _snapshot(harness) != before


@pytest.mark.parametrize("write", [_fork, _item_outcome, _transform_error], ids=lambda write: write.__name__)
def test_reclaimed_item_refuses_without_payload_mutation(harness: _Harness, write: Callable[[_Harness, TokenWorkItem], None]) -> None:
    item = _claim(harness)
    with harness.db.engine.begin() as conn:
        conn.execute(
            update(token_work_items_table)
            .where(token_work_items_table.c.work_item_id == item.work_item_id)
            .values(attempt=item.attempt + 1)
        )
    before = _snapshot(harness)
    with pytest.raises(SchedulerLeaseLostError):
        write(harness, item)
    assert _snapshot(harness) == before


@pytest.mark.parametrize("stale", [False, True])
def test_collector_release_is_leader_fenced(harness: _Harness, stale: bool) -> None:
    h = harness
    children, group_id = h.factory.data_flow.expand_token(
        h.ref,
        h.token.row_id,
        [{"value": 2}],
        output_contract=_CONTRACT,
        member_token=h.member,
    )
    refs = [TokenRef(child.token_id, h.leader.run_id) for child in children]
    before = _snapshot(h)
    if stale:
        with pytest.raises(RunLeadershipLostError):
            h.factory.data_flow.collect_tokens(
                refs,
                group_id,
                "collector",
                [{"value": 3}],
                [_CONTRACT],
                coordination_token=replace(h.leader, leader_epoch=h.leader.leader_epoch + 1),
            )
        assert _snapshot(h) == before
    else:
        h.factory.data_flow.collect_tokens(refs, group_id, "collector", [{"value": 3}], [_CONTRACT], coordination_token=h.leader)
        assert _snapshot(h) != before


@pytest.mark.parametrize("stale", [False, True])
def test_coalesce_materialization_is_leader_fenced(harness: _Harness, stale: bool) -> None:
    h = harness
    children, _ = h.factory.data_flow.fork_token(h.ref, h.token.row_id, ["left", "right"], member_token=h.member, work_item=_claim(h))
    refs = [TokenRef(child.token_id, h.leader.run_id) for child in children]
    before = _snapshot(h)
    if stale:
        with pytest.raises(RunLeadershipLostError):
            h.factory.data_flow.coalesce_tokens(
                refs,
                h.token.row_id,
                {"value": 3},
                merged_contract=_CONTRACT,
                coordination_token=replace(h.leader, leader_epoch=h.leader.leader_epoch + 1),
            )
        assert _snapshot(h) == before
    else:
        h.factory.data_flow.coalesce_tokens(refs, h.token.row_id, {"value": 3}, merged_contract=_CONTRACT, coordination_token=h.leader)
        assert _snapshot(h) != before


@pytest.mark.parametrize("write", [_fork, _item_outcome, _transform_error], ids=lambda write: write.__name__)
def test_claim_cannot_authorize_a_different_token(harness: _Harness, write: Callable[[_Harness, TokenWorkItem], None]) -> None:
    h = harness
    item = _claim(h)
    other = h.factory.data_flow.create_token(h.token.row_id, coordination_token=h.leader)
    before = _snapshot(h)
    with pytest.raises(AuditIntegrityError, match="claimed work item"):
        write(replace(h, token=other), item)
    assert _snapshot(h) == before


@pytest.mark.parametrize("stale", [False, True])
def test_coalesce_finalization_is_leader_fenced(harness: _Harness, stale: bool) -> None:
    h = harness
    h.factory.data_flow.register_node(
        "coalesce",
        NodeType.COALESCE,
        "1.0",
        {},
        node_id="coalesce",
        schema_config=_SCHEMA,
        coordination_token=h.leader,
    )
    children, _ = h.factory.data_flow.fork_token(h.ref, h.token.row_id, ["left", "right"], member_token=h.member, work_item=_claim(h))
    refs = [TokenRef(child.token_id, h.leader.run_id) for child in children]
    states = [
        h.factory.execution.node_states.begin_node_state(child.token_id, "coalesce", 1, {}, member_token=h.member) for child in children
    ]
    merged = h.factory.data_flow.coalesce_tokens(
        refs,
        h.token.row_id,
        {"value": 3},
        merged_contract=_CONTRACT,
        coordination_token=h.leader,
        coalesce_node_id="coalesce",
        parent_state_ids=[state.state_id for state in states],
    )
    completions = [CoalesceParentCompletion(ref, state.state_id, 1.0, None) for ref, state in zip(refs, states, strict=True)]
    before = _snapshot(h)
    if stale:
        with pytest.raises(RunLeadershipLostError):
            h.factory.data_flow.finalize_coalesce_effect(
                merged=merged,
                parent_completions=completions,
                coordination_token=replace(h.leader, leader_epoch=h.leader.leader_epoch + 1),
            )
        assert _snapshot(h) == before
    else:
        h.factory.data_flow.finalize_coalesce_effect(merged=merged, parent_completions=completions, coordination_token=h.leader)
        assert _snapshot(h) != before
        with h.db.read_only_connection() as conn:
            terminal_parents = conn.execute(
                select(token_outcomes_table.c.token_id, token_outcomes_table.c.path)
                .where(token_outcomes_table.c.run_id == h.leader.run_id)
                .where(token_outcomes_table.c.token_id.in_([ref.token_id for ref in refs]))
            ).all()
            completed_states = conn.execute(
                select(node_states_table.c.state_id, node_states_table.c.status).where(
                    node_states_table.c.state_id.in_([state.state_id for state in states])
                )
            ).all()
        assert set(terminal_parents) == {(ref.token_id, TerminalPath.COALESCED.value) for ref in refs}
        assert set(completed_states) == {(state.state_id, "completed") for state in states}


def test_bulk_outcome_conflict_rolls_back_earlier_insert(harness: _Harness) -> None:
    h = harness
    other = h.factory.data_flow.create_token(h.token.row_id, coordination_token=h.leader)
    first = BarrierTerminalOutcomeSpec(h.token.token_id, TerminalOutcome.SUCCESS, TerminalPath.COALESCED)
    second = BarrierTerminalOutcomeSpec(other.token_id, TerminalOutcome.SUCCESS, TerminalPath.COALESCED)
    with fenced_leader_transaction(h.db.engine, token=h.leader, window_seconds=60, verb="test_bulk") as conn:
        record_terminal_outcomes_guarded(conn, run_id=h.leader.run_id, outcomes=[second], recorded_at=h.token.created_at)
    before = _snapshot(h)
    with (
        pytest.raises(LandscapeRecordError),
        fenced_leader_transaction(h.db.engine, token=h.leader, window_seconds=60, verb="test_bulk") as conn,
    ):
        record_terminal_outcomes_guarded(conn, run_id=h.leader.run_id, outcomes=[first, second], recorded_at=h.token.created_at)
    assert _snapshot(h) == before


def test_coalesce_finalization_late_parent_conflict_rolls_back_all_evidence(harness: _Harness) -> None:
    h = harness
    h.factory.data_flow.register_node(
        "coalesce",
        NodeType.COALESCE,
        "1.0",
        {},
        node_id="coalesce",
        schema_config=_SCHEMA,
        coordination_token=h.leader,
    )
    children, _ = h.factory.data_flow.fork_token(h.ref, h.token.row_id, ["left", "right"], member_token=h.member, work_item=_claim(h))
    refs = [TokenRef(child.token_id, h.leader.run_id) for child in children]
    states = [
        h.factory.execution.node_states.begin_node_state(child.token_id, "coalesce", 1, {}, member_token=h.member) for child in children
    ]
    # Leave state membership unbound so the failed transaction also has to
    # roll back its batched membership CAS, in addition to node-state writes.
    merged = h.factory.data_flow.coalesce_tokens(
        refs,
        h.token.row_id,
        {"value": 3},
        merged_contract=_CONTRACT,
        coordination_token=h.leader,
    )
    h.factory.data_flow.record_token_outcome_leader(
        refs[1],
        TerminalOutcome.SUCCESS,
        TerminalPath.COALESCED,
        coordination_token=h.leader,
    )
    completions = [CoalesceParentCompletion(ref, state.state_id, 1.0, None) for ref, state in zip(refs, states, strict=True)]
    before = _snapshot(h)
    with pytest.raises(AuditIntegrityError, match="database rejected audit write"):
        h.factory.data_flow.finalize_coalesce_effect(
            merged=merged,
            parent_completions=completions,
            coordination_token=h.leader,
        )
    assert _snapshot(h) == before


def test_bulk_outcome_validates_entire_batch_before_inserting(harness: _Harness) -> None:
    h = harness
    other = h.factory.data_flow.create_token(h.token.row_id, coordination_token=h.leader)
    first = BarrierTerminalOutcomeSpec(h.token.token_id, TerminalOutcome.SUCCESS, TerminalPath.COALESCED)
    invalid = BarrierTerminalOutcomeSpec(other.token_id, TerminalOutcome.FAILURE, TerminalPath.UNROUTED)
    before = _snapshot(h)
    with fenced_leader_transaction(h.db.engine, token=h.leader, window_seconds=60, verb="test_bulk") as conn, pytest.raises(ValueError):
        record_terminal_outcomes_guarded(conn, run_id=h.leader.run_id, outcomes=[first, invalid], recorded_at=h.token.created_at)
    assert _snapshot(h) == before


def test_general_outcome_batch_preserves_order_and_context(harness: _Harness) -> None:
    h = harness
    other = h.factory.data_flow.create_token(h.token.row_id, coordination_token=h.leader)
    writes = (
        TokenOutcomeWrite(h.ref, None, TerminalPath.ABANDONED, context={"reason": "closed", "arms": ["source"]}),
        TokenOutcomeWrite(
            TokenRef(other.token_id, h.leader.run_id),
            TerminalOutcome.SUCCESS,
            TerminalPath.DEFAULT_FLOW,
            sink_name="output",
            context={"rows": 1},
        ),
    )
    with fenced_leader_transaction(h.db.engine, token=h.leader, window_seconds=60, verb="test_general_batch") as conn:
        ids = h.factory.data_flow.outcomes.record_token_outcomes_on(conn, run_id=h.leader.run_id, outcomes=writes)
    with h.db.read_only_connection() as conn:
        persisted = {row.outcome_id: row for row in conn.execute(select(token_outcomes_table))}
    assert len(ids) == 2
    assert [persisted[outcome_id].token_id for outcome_id in ids] == [h.token.token_id, other.token_id]
    assert persisted[ids[0]].outcome is None
    assert persisted[ids[0]].completed == 0
    assert json.loads(persisted[ids[0]].context_json) == {"reason": "closed", "arms": ["source"]}
    assert persisted[ids[1]].sink_name == "output"
    assert persisted[ids[1]].completed == 1
    assert json.loads(persisted[ids[1]].context_json) == {"rows": 1}


def test_outcome_write_freezes_nested_context_before_batch_persistence(harness: _Harness) -> None:
    h = harness
    detail = {"reason": "original"}
    steps = [detail]
    context = {"steps": steps}
    request = TokenOutcomeWrite(h.ref, None, TerminalPath.ABANDONED, context=context)

    detail["reason"] = "changed after request creation"
    steps.append({"reason": "later step"})
    context["steps"] = []

    assert request.context == {"steps": ({"reason": "original"},)}
    with fenced_leader_transaction(h.db.engine, token=h.leader, window_seconds=60, verb="test_frozen_outcome") as conn:
        outcome_ids = h.factory.data_flow.outcomes.record_token_outcomes_on(conn, run_id=h.leader.run_id, outcomes=(request,))
    with h.db.read_only_connection() as conn:
        recorded = conn.execute(
            select(token_outcomes_table.c.context_json).where(token_outcomes_table.c.outcome_id == outcome_ids[0])
        ).scalar_one()
    assert json.loads(recorded) == {"steps": [{"reason": "original"}]}


@pytest.mark.parametrize("abandon_first", [True, False])
def test_general_outcome_batch_refuses_intra_batch_abandonment_contradiction(harness: _Harness, abandon_first: bool) -> None:
    h = harness
    abandoned = TokenOutcomeWrite(h.ref, None, TerminalPath.ABANDONED)
    decided = TokenOutcomeWrite(h.ref, TerminalOutcome.SUCCESS, TerminalPath.COALESCED)
    writes = (abandoned, decided) if abandon_first else (decided, abandoned)
    before = _snapshot(h)
    # Catch within the transaction: rejection must occur before any INSERT.
    with (
        fenced_leader_transaction(h.db.engine, token=h.leader, window_seconds=60, verb="test_general_batch") as conn,
        pytest.raises(AuditIntegrityError, match="decided-plus-abandoned"),
    ):
        h.factory.data_flow.outcomes.record_token_outcomes_on(conn, run_id=h.leader.run_id, outcomes=writes)
    assert _snapshot(h) == before


def test_general_outcome_batch_keeps_sink_witness_validation(harness: _Harness) -> None:
    h = harness
    other = h.factory.data_flow.create_token(h.token.row_id, coordination_token=h.leader)
    writes = (
        TokenOutcomeWrite(h.ref, TerminalOutcome.SUCCESS, TerminalPath.COALESCED),
        TokenOutcomeWrite(
            TokenRef(other.token_id, h.leader.run_id),
            TerminalOutcome.TRANSIENT,
            TerminalPath.SINK_FALLBACK_TO_FAILSINK,
            sink_name="errors",
            error_hash="e" * 64,
        ),
    )
    before = _snapshot(h)
    with (
        fenced_leader_transaction(h.db.engine, token=h.leader, window_seconds=60, verb="test_general_batch") as conn,
        pytest.raises(AuditIntegrityError, match=r"I1c.*node_id"),
    ):
        h.factory.data_flow.outcomes.record_token_outcomes_on(conn, run_id=h.leader.run_id, outcomes=writes)
    assert _snapshot(h) == before


@pytest.mark.parametrize("helper", ["fetchone", "fetchone_without_connection", "lock_query"])
@pytest.mark.parametrize("mutation", ["insert", "update", "delete"])
def test_outcome_read_helpers_refuse_dml(harness: _Harness, helper: str, mutation: str) -> None:
    h = harness
    statements = {
        "insert": token_outcomes_table.insert(),
        "update": tokens_table.update().values(step_in_pipeline=99),
        "delete": tokens_table.delete(),
    }
    # Deliberately cross the nominal query boundary to prove runtime refusal,
    # including the fetch helper's connection-provider execution arm.
    query = cast(Select[Any], statements[mutation])
    before = _snapshot(h)
    with (
        fenced_leader_transaction(h.db.engine, token=h.leader, window_seconds=60, verb="test_read_helper") as conn,
        pytest.raises(TypeError, match="requires a SQLAlchemy Select"),
    ):
        if helper == "lock_query":
            h.factory.data_flow.outcomes._execute_lock_query(conn, query, operation="test")
        else:
            h.factory.data_flow.outcomes._execute_fetchone(query, conn=None if helper == "fetchone_without_connection" else conn)
    assert _snapshot(h) == before


@pytest.mark.parametrize("helper", ["fetchone", "fetchone_without_connection", "lock_query"])
@pytest.mark.parametrize("fragment", ["insert_cte", "update_cte", "delete_cte", "text_column", "text_predicate"])
def test_outcome_read_helpers_refuse_writes_and_text_inside_select(harness: _Harness, helper: str, fragment: str) -> None:
    h = harness
    statements = {
        "insert_cte": select(tokens_table.c.token_id).add_cte(tokens_table.insert().returning(tokens_table.c.token_id).cte()),
        "update_cte": select(tokens_table.c.token_id).add_cte(
            tokens_table.update().values(step_in_pipeline=99).returning(tokens_table.c.token_id).cte()
        ),
        "delete_cte": select(tokens_table.c.token_id).add_cte(tokens_table.delete().returning(tokens_table.c.token_id).cte()),
        "text_column": select(text("token_id")).select_from(tokens_table),
        "text_predicate": select(tokens_table.c.token_id).where(text("step_in_pipeline IS NULL")),
    }
    query = statements[fragment]
    before = _snapshot(h)
    with (
        fenced_leader_transaction(h.db.engine, token=h.leader, window_seconds=60, verb="test_read_helper") as conn,
        pytest.raises(TypeError, match="contains DML or raw SQL"),
    ):
        if helper == "lock_query":
            h.factory.data_flow.outcomes._execute_lock_query(conn, query, operation="test")
        else:
            h.factory.data_flow.outcomes._execute_fetchone(query, conn=None if helper == "fetchone_without_connection" else conn)
    assert _snapshot(h) == before


@pytest.mark.parametrize("helper", ["fetchone", "fetchone_without_connection", "lock_query"])
def test_outcome_read_helpers_accept_plain_and_for_update_select(harness: _Harness, helper: str) -> None:
    h = harness
    query = select(tokens_table.c.token_id).where(tokens_table.c.token_id == h.token.token_id)
    before = _snapshot(h)
    if helper == "fetchone_without_connection":
        # This branch owns its transaction and must not nest inside another
        # transaction on the in-memory SQLite fixture's shared connection.
        row = h.factory.data_flow.outcomes._execute_fetchone(query, conn=None)
        assert row.token_id == h.token.token_id
    else:
        query = query.with_for_update(of=tokens_table)
        with fenced_leader_transaction(h.db.engine, token=h.leader, window_seconds=60, verb="test_read_helper") as conn:
            if helper == "lock_query":
                rows = h.factory.data_flow.outcomes._execute_lock_query(conn, query, operation="test")
                assert [row.token_id for row in rows] == [h.token.token_id]
            else:
                row = h.factory.data_flow.outcomes._execute_fetchone(query, conn=conn)
                assert row.token_id == h.token.token_id
    assert _snapshot(h) == before


@pytest.mark.parametrize("write", _LEADER_WRITES, ids=lambda write: write.__name__)
def test_member_token_cannot_substitute_for_leader(harness: _Harness, write: Callable[[_Harness, CoordinationToken], None]) -> None:
    before = _snapshot(harness)
    with pytest.raises(TypeError):
        write(harness, cast(CoordinationToken, harness.member))
    assert _snapshot(harness) == before


@pytest.mark.parametrize("write", [_contract, _expand, _empty], ids=lambda write: write.__name__)
def test_leader_token_cannot_substitute_for_membership(harness: _Harness, write: Callable[[_Harness, WorkerMembershipToken], None]) -> None:
    before = _snapshot(harness)
    with pytest.raises(TypeError):
        write(harness, cast(WorkerMembershipToken, harness.leader))
    assert _snapshot(harness) == before
