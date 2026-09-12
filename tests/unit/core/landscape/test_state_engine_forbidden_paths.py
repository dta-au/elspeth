"""Complete-image refusal contracts for state-engine forbidden paths.

This module owns the Task 7 forbidden-path cohort only: F-04, F-06, F-07,
F-10, and F-12.  Every behavioral refusal compares the complete run-owned
durable image.  A valid leader-fenced no-op may extend the leader seat, while
a stale leader may add only its contracted best-effort ``fence_refusal``
event; neither delta is mislabeled as payload mutation.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts import (
    AggregationResultMember,
    BatchStatus,
    NodeStateStatus,
    NodeType,
    OutputMode,
    RoutingMode,
    RunStatus,
    TerminalOutcome,
    TerminalPath,
    TriggerType,
)
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.enums import AggregationMemberAction
from elspeth.contracts.errors import (
    AuditIntegrityError,
    RunLeadershipLostError,
    RunMembershipLostError,
)
from elspeth.contracts.node_state_context import AggregationFlushContext
from elspeth.contracts.scheduler import BarrierEmission, TokenWorkStatus
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape import database as database_module
from elspeth.core.landscape.data_flow.outcomes import record_buffered_outcome_guarded
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.execution.batches import add_batch_member_guarded
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    run_coordination_table,
    run_workers_table,
    token_work_items_table,
)
from tests.fixtures.landscape import expire_lease, leader_coordination_token, make_factory, make_landscape_db, register_test_node
from tests.helpers.state_engine import StateEngineImage, capture_state_engine_image
from tests.helpers.tree_gate import iter_gate_sources

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
RUN_ID = "forbidden-path-run"
LEADER = f"worker:{RUN_ID}:leader"
WRONG_OWNER = f"worker:{RUN_ID}:wrong-owner"
SOURCE_NODE_ID = "source-1"
NODE_ID = "transform-1"
ROOT = Path(__file__).resolve().parents[4]
PAYLOAD = TokenSchedulerRepository.serialize_row_payload(PipelineRow({"value": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True)))


@dataclass(frozen=True)
class _Harness:
    db: LandscapeDB
    factory: RecorderFactory
    repo: TokenSchedulerRepository
    coordination_token: CoordinationToken


@pytest.fixture
def harness() -> Iterator[_Harness]:
    db = make_landscape_db()
    factory = make_factory(db)
    factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id=RUN_ID,
        leader_worker_id=LEADER,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    factory.data_flow.register_node(
        plugin_name="source",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        node_id=SOURCE_NODE_ID,
        schema_config=SchemaConfig.from_dict({"mode": "observed"}),
        coordination_token=leader_coordination_token(factory, RUN_ID),
    )
    register_test_node(factory.data_flow, RUN_ID, NODE_ID)
    try:
        yield _Harness(
            db=db,
            factory=factory,
            repo=factory.scheduler,
            coordination_token=leader_coordination_token(factory, RUN_ID),
        )
    finally:
        db.close()


def _enqueue(harness: _Harness, name: str, sequence: int) -> tuple[str, str, str]:
    row, token = harness.factory.data_flow.create_row_with_token(
        source_node_id=SOURCE_NODE_ID,
        row_index=sequence,
        data={"name": name},
        source_row_index=sequence,
        ingest_sequence=sequence,
        row_id=f"row-{name}",
        token_id=f"token-{name}",
        coordination_token=leader_coordination_token(harness.factory, RUN_ID),
    )
    item = harness.repo.enqueue_ready(
        member_token=harness.coordination_token.membership,
        token_id=token.token_id,
        row_id=row.row_id,
        node_id=NODE_ID,
        step_index=1,
        ingest_sequence=sequence,
        row_payload_json=PAYLOAD,
    )
    return row.row_id, token.token_id, item.work_item_id


def _claim(harness: _Harness, name: str = "parent", *, owner: str = LEADER) -> tuple[str, str, str]:
    row_id, token_id, work_item_id = _enqueue(harness, name, 0)
    claimed = harness.repo.claim_ready(
        member_token=WorkerMembershipToken(run_id=RUN_ID, worker_id=owner), lease_owner=owner, lease_seconds=60
    )
    assert claimed is not None and claimed.work_item_id == work_item_id
    return row_id, token_id, work_item_id


def _ready_child(harness: _Harness) -> BarrierEmission:
    row, token = harness.factory.data_flow.create_row_with_token(
        source_node_id=SOURCE_NODE_ID,
        row_index=1,
        data={"name": "child"},
        source_row_index=1,
        ingest_sequence=1,
        row_id="row-child",
        token_id="token-child",
        coordination_token=leader_coordination_token(harness.factory, RUN_ID),
    )
    return BarrierEmission(
        token_id=token.token_id,
        row_id=row.row_id,
        node_id=NODE_ID,
        step_index=2,
        ingest_sequence=1,
        row_payload_json=PAYLOAD,
    )


def _mark_blocked(repo: TokenSchedulerRepository, work_item_id: str, _: BarrierEmission | None) -> object:
    return repo.mark_blocked(
        member_token=WorkerMembershipToken(run_id=RUN_ID, worker_id=WRONG_OWNER),
        work_item_id=work_item_id,
        queue_key="queue-a",
        barrier_key=None,
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_terminal(repo: TokenSchedulerRepository, work_item_id: str, _: BarrierEmission | None) -> object:
    return repo.mark_terminal(
        member_token=WorkerMembershipToken(run_id=RUN_ID, worker_id=WRONG_OWNER),
        work_item_id=work_item_id,
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_failed(repo: TokenSchedulerRepository, work_item_id: str, _: BarrierEmission | None) -> object:
    return repo.mark_failed(
        member_token=WorkerMembershipToken(run_id=RUN_ID, worker_id=WRONG_OWNER),
        work_item_id=work_item_id,
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_pending_sink(repo: TokenSchedulerRepository, work_item_id: str, _: BarrierEmission | None) -> object:
    return repo.mark_pending_sink(
        member_token=WorkerMembershipToken(run_id=RUN_ID, worker_id=WRONG_OWNER),
        work_item_id=work_item_id,
        row_payload_json=PAYLOAD,
        sink_name="sink-a",
        outcome=TerminalOutcome.SUCCESS.value,
        path=TerminalPath.DEFAULT_FLOW.value,
        error_hash=None,
        error_message=None,
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_terminal_with_ready_children(
    repo: TokenSchedulerRepository,
    work_item_id: str,
    child: BarrierEmission | None,
) -> object:
    assert child is not None
    return repo.mark_terminal_with_ready_children(
        member_token=WorkerMembershipToken(run_id=RUN_ID, worker_id=WRONG_OWNER),
        work_item_id=work_item_id,
        emitted_ready=(child,),
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_failed_with_ready_children(
    repo: TokenSchedulerRepository,
    work_item_id: str,
    child: BarrierEmission | None,
) -> object:
    assert child is not None
    return repo.mark_failed_with_ready_children(
        member_token=WorkerMembershipToken(run_id=RUN_ID, worker_id=WRONG_OWNER),
        work_item_id=work_item_id,
        emitted_ready=(child,),
        expected_lease_owner=WRONG_OWNER,
    )


def _mark_pending_sink_with_ready_children(
    repo: TokenSchedulerRepository,
    work_item_id: str,
    child: BarrierEmission | None,
) -> object:
    assert child is not None
    return repo.mark_pending_sink_with_ready_children(
        member_token=WorkerMembershipToken(run_id=RUN_ID, worker_id=WRONG_OWNER),
        work_item_id=work_item_id,
        emitted_ready=(child,),
        row_payload_json=PAYLOAD,
        sink_name="sink-a",
        outcome=TerminalOutcome.SUCCESS.value,
        path=TerminalPath.DEFAULT_FLOW.value,
        error_hash=None,
        error_message=None,
        expected_lease_owner=WRONG_OWNER,
    )


_WrongOwnerDisposition = Callable[[TokenSchedulerRepository, str, BarrierEmission | None], object]


@pytest.mark.parametrize(
    ("disposition", "needs_child"),
    (
        pytest.param(_mark_blocked, False, id="F-04-mark-blocked"),
        pytest.param(_mark_terminal, False, id="F-04-mark-terminal"),
        pytest.param(_mark_failed, False, id="F-04-mark-failed"),
        pytest.param(_mark_pending_sink, False, id="F-04-mark-pending-sink"),
        pytest.param(_mark_terminal_with_ready_children, True, id="F-04-mark-terminal-with-ready-children"),
        pytest.param(_mark_failed_with_ready_children, True, id="F-04-mark-failed-with-ready-children"),
        pytest.param(_mark_pending_sink_with_ready_children, True, id="F-04-mark-pending-sink-with-ready-children"),
    ),
)
def test_f04_wrong_owner_cannot_disposition_a_lease(
    harness: _Harness,
    disposition: _WrongOwnerDisposition,
    needs_child: bool,
) -> None:
    _register_peer(harness, WRONG_OWNER)
    _row_id, _token_id, work_item_id = _claim(harness)
    child = _ready_child(harness) if needs_child else None
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    with pytest.raises(AuditIntegrityError, match=r"expected lease_owner.*wrong-owner"):
        disposition(harness.repo, work_item_id, child)

    assert capture_state_engine_image(harness.db, run_id=RUN_ID) == before


@pytest.mark.parametrize("batch", (False, True), ids=("singleton", "batch"))
def test_f04_wrong_owner_cannot_terminalize_pending_sink_debt_without_mutation(
    harness: _Harness,
    batch: bool,
) -> None:
    _row_id, token_id, work_item_id = _claim(harness)
    harness.repo.mark_pending_sink(
        member_token=harness.coordination_token.membership,
        work_item_id=work_item_id,
        row_payload_json=PAYLOAD,
        sink_name="sink-a",
        outcome=TerminalOutcome.SUCCESS.value,
        path=TerminalPath.DEFAULT_FLOW.value,
        error_hash=None,
        error_message=None,
        expected_lease_owner=LEADER,
    )
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    if batch:
        with pytest.raises(AuditIntegrityError, match="strict owner CAS"):
            harness.repo.mark_pending_sink_terminal_many(
                token_ids=(token_id,),
                expected_lease_owner=WRONG_OWNER,
                coordination_token=harness.coordination_token,
            )
    else:
        assert (
            harness.repo.mark_pending_sink_terminal(
                token_id=token_id,
                expected_lease_owner=WRONG_OWNER,
                coordination_token=harness.coordination_token,
            )
            == 0
        )

    assert capture_state_engine_image(harness.db, run_id=RUN_ID) == before


@pytest.mark.parametrize("status", ("departed", "evicted"))
@pytest.mark.parametrize("subtype", ("ready", "pending_sink"))
def test_f06_inactive_registered_worker_cannot_claim(
    harness: _Harness,
    status: str,
    subtype: str,
) -> None:
    _row_id, _token_id, work_item_id = _enqueue(harness, "claim", 0)
    if subtype == "pending_sink":
        claimed = harness.repo.claim_ready(member_token=harness.coordination_token.membership, lease_owner=LEADER, lease_seconds=60)
        assert claimed is not None
        harness.repo.mark_pending_sink(
            member_token=harness.coordination_token.membership,
            work_item_id=work_item_id,
            row_payload_json=PAYLOAD,
            sink_name="sink-a",
            outcome=TerminalOutcome.SUCCESS.value,
            path=TerminalPath.DEFAULT_FLOW.value,
            error_hash=None,
            error_message=None,
            expected_lease_owner=LEADER,
        )
    inactive = f"worker:{RUN_ID}:{status}"
    with harness.db.engine.begin() as conn:
        conn.execute(
            insert(run_workers_table).values(
                worker_id=inactive,
                run_id=RUN_ID,
                role="follower",
                status=status,
                registered_at=NOW,
                heartbeat_expires_at=NOW + timedelta(hours=1),
                departed_at=NOW if status == "departed" else None,
                evicted_at=NOW if status == "evicted" else None,
            )
        )
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    if subtype == "ready":
        with pytest.raises(RunMembershipLostError) as raised:
            harness.repo.claim_ready(
                member_token=WorkerMembershipToken(run_id=RUN_ID, worker_id=inactive), lease_owner=inactive, lease_seconds=60
            )
        assert raised.value.worker_id == inactive
        assert raised.value.run_id == RUN_ID
        _assert_only_fence_refusal(harness, before, verb="claim_ready", membership=True)
    else:
        with pytest.raises(RunLeadershipLostError):
            harness.repo.claim_pending_sink(
                coordination_token=CoordinationToken(run_id=RUN_ID, worker_id=inactive, leader_epoch=1),
                lease_owner=inactive,
                lease_seconds=60,
            )
        _assert_only_fence_refusal(harness, before, verb="claim_pending_sink")


@pytest.mark.parametrize("subtype", ("ready", "pending_sink"))
def test_f06_absent_worker_claim_is_refused_without_payload_mutation(subtype: str, harness: _Harness) -> None:
    _row_id, _token_id, work_item_id = _enqueue(harness, "claim", 0)
    if subtype == "pending_sink":
        claimed = harness.repo.claim_ready(member_token=harness.coordination_token.membership, lease_owner=LEADER, lease_seconds=60)
        assert claimed is not None
        harness.repo.mark_pending_sink(
            member_token=harness.coordination_token.membership,
            work_item_id=work_item_id,
            row_payload_json=PAYLOAD,
            sink_name="sink-a",
            outcome=TerminalOutcome.SUCCESS.value,
            path=TerminalPath.DEFAULT_FLOW.value,
            error_hash=None,
            error_message=None,
            expected_lease_owner=LEADER,
        )
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    if subtype == "ready":
        with pytest.raises(AuditIntegrityError, match="unregistered"):
            harness.repo.claim_ready(
                member_token=WorkerMembershipToken(run_id=RUN_ID, worker_id="unregistered"), lease_owner="unregistered", lease_seconds=60
            )
        assert capture_state_engine_image(harness.db, run_id=RUN_ID) == before
    else:
        with pytest.raises(RunLeadershipLostError):
            harness.repo.claim_pending_sink(
                coordination_token=CoordinationToken(run_id=RUN_ID, worker_id="unregistered", leader_epoch=1),
                lease_owner="unregistered",
                lease_seconds=60,
            )
        _assert_only_fence_refusal(harness, before, verb="claim_pending_sink")


def _register_peer(harness: _Harness, peer: str, *, status: str = "active") -> None:
    with harness.db.engine.begin() as conn:
        conn.execute(
            insert(run_workers_table).values(
                worker_id=peer,
                run_id=RUN_ID,
                role="follower",
                status=status,
                registered_at=NOW,
                heartbeat_expires_at=read_landscape_transaction_time(conn) + timedelta(minutes=10),
                departed_at=NOW if status == "departed" else None,
            )
        )


def _age_leader_seat(harness: _Harness) -> None:
    """Pull the seat's deadline and stamp back inside the window so the fence refresh is observable.

    The seat is minted on Landscape database time (ADR-047) and the leader
    fence re-stamps it from the same clock, so a fenced verb that runs inside
    the seat's minting second would rewrite identical values and the
    "exactly the fence delta" proof below would see no delta at all.
    """
    with harness.db.engine.begin() as conn:
        database_now = read_landscape_transaction_time(conn)
        conn.execute(
            update(run_coordination_table)
            .where(run_coordination_table.c.run_id == RUN_ID)
            .values(leader_heartbeat_expires_at=database_now + timedelta(seconds=40), updated_at=database_now - timedelta(seconds=5))
        )


def _assert_only_leader_heartbeat_delta(before: StateEngineImage, after: StateEngineImage) -> None:
    delta = before.diff(after)
    assert delta.changed_tables == {"run_coordination"}
    assert delta.changed_columns == {
        "run_coordination": {"leader_heartbeat_expires_at", "updated_at"},
    }


@pytest.mark.parametrize(
    ("owner", "worker_status", "expired_seconds_ago"),
    (
        pytest.param(LEADER, None, 61, id="F-07-own-expired-lease"),
        pytest.param("live-peer", "active", 1, id="F-07-live-peer-fresh-expiry"),
        pytest.param("dead-peer", "departed", None, id="F-07-not-yet-expired"),
    ),
)
def test_f07_ineligible_lease_is_not_recovered(
    harness: _Harness,
    owner: str,
    worker_status: str | None,
    expired_seconds_ago: int | None,
) -> None:
    """Three leases the sweep must leave LEASED: the caller's own expired lease,
    a registry-live peer's lease expired inside the stall budget, and a dead
    peer's lease that has not expired. Expiry is aged through the database
    (ADR-047); ``None`` leaves the 60 s lease live."""
    if worker_status is not None:
        _register_peer(harness, owner)
    _row_id, _token_id, work_item_id = _claim(harness, owner=owner)
    if expired_seconds_ago is not None:
        expire_lease(harness.db.engine, work_item_id, seconds_ago=expired_seconds_ago)
    if worker_status == "departed":
        with harness.db.engine.begin() as conn:
            conn.execute(
                update(run_workers_table)
                .where(run_workers_table.c.worker_id == owner)
                .where(run_workers_table.c.run_id == RUN_ID)
                .values(status="departed", departed_at=NOW)
            )
    _age_leader_seat(harness)
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    recovered = harness.repo.recover_expired_leases(
        coordination_token=harness.coordination_token,
    )

    assert recovered == 0
    after = capture_state_engine_image(harness.db, run_id=RUN_ID)
    _assert_only_leader_heartbeat_delta(before, after)
    work = next(row for row in after.tables["token_work_items"] if row["work_item_id"] == work_item_id)
    assert work["status"] == TokenWorkStatus.LEASED.value
    assert work["lease_owner"] == owner


def _depose_leader(harness: _Harness) -> StateEngineImage:
    with harness.db.engine.begin() as conn:
        conn.execute(
            update(run_coordination_table)
            .where(run_coordination_table.c.run_id == RUN_ID)
            .values(leader_epoch=run_coordination_table.c.leader_epoch + 1)
        )
    return capture_state_engine_image(harness.db, run_id=RUN_ID)


def _assert_only_fence_refusal(
    harness: _Harness,
    before: StateEngineImage,
    *,
    verb: str,
    membership: bool = False,
) -> None:
    after = capture_state_engine_image(harness.db, run_id=RUN_ID)
    delta = before.diff(after)
    assert delta.changed_tables == {"run_coordination_events"}
    added = tuple(row for row in after.tables["run_coordination_events"] if row not in before.tables["run_coordination_events"])
    assert len(added) == 1
    assert added[0]["event_type"] == "fence_refusal"
    expected_context = {"verb": verb, "fence": "membership"} if membership else {"verb": verb}
    assert json.loads(str(added[0]["context_json"])) == expected_context
    assert after.tables["runs"] == before.tables["runs"]


def test_f10_stale_leader_refusal_adds_only_fence_refusal_event(harness: _Harness) -> None:
    before = _depose_leader(harness)

    with pytest.raises(RunLeadershipLostError) as raised:
        harness.factory.run_lifecycle.update_run_status(
            RunStatus.FAILED,
            coordination_token=harness.coordination_token,
        )

    assert raised.value.verb == "update_run_status"
    _assert_only_fence_refusal(harness, before, verb="update_run_status")


def test_f10_stale_leader_cannot_reconcile_source_completions(harness: _Harness) -> None:
    """The TS-02 repair surface is leader-fenced like every other repair verb."""
    before = _depose_leader(harness)

    with pytest.raises(RunLeadershipLostError) as raised:
        harness.factory.execution.reconcile_source_completions_from_scheduler(
            coordination_token=harness.coordination_token,
        )

    assert raised.value.verb == "reconcile_source_completions_from_scheduler"
    _assert_only_fence_refusal(harness, before, verb="reconcile_source_completions_from_scheduler")


@pytest.mark.parametrize(
    ("disposition", "needs_child"),
    (
        pytest.param(_mark_terminal, False, id="terminal"),
        pytest.param(_mark_failed, False, id="failed"),
        pytest.param(_mark_blocked, False, id="blocked"),
        pytest.param(_mark_pending_sink, False, id="pending-sink"),
        pytest.param(_mark_terminal_with_ready_children, True, id="terminal-children"),
        pytest.param(_mark_failed_with_ready_children, True, id="failed-children"),
        pytest.param(_mark_pending_sink_with_ready_children, True, id="pending-sink-children"),
    ),
)
def test_f10_evicted_member_cannot_disposition_owned_work(
    harness: _Harness, disposition: _WrongOwnerDisposition, needs_child: bool
) -> None:
    _register_peer(harness, WRONG_OWNER)
    _, _, work_item_id = _claim(harness, owner=WRONG_OWNER)
    child = _ready_child(harness) if needs_child else None
    with harness.db.engine.begin() as conn:
        conn.execute(update(run_workers_table).where(run_workers_table.c.worker_id == WRONG_OWNER).values(status="evicted", evicted_at=NOW))
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)
    with pytest.raises(RunMembershipLostError):
        disposition(harness.repo, work_item_id, child)
    verb = "_transition_with_ready_children" if needs_child else "_transition"
    _assert_only_fence_refusal(harness, before, verb=verb, membership=True)


def test_f10_evicted_member_cannot_record_routing_event(harness: _Harness) -> None:
    _, token_id, _ = _claim(harness)
    state = harness.factory.execution.begin_node_state(token_id, NODE_ID, 1, {}, member_token=harness.coordination_token.membership)
    with harness.db.engine.begin() as conn:
        conn.execute(update(run_workers_table).where(run_workers_table.c.worker_id == LEADER).values(status="evicted", evicted_at=NOW))
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)
    with pytest.raises(RunMembershipLostError):
        harness.factory.execution.record_routing_event(
            state.state_id, "unreached-edge", RoutingMode.MOVE, member_token=harness.coordination_token.membership
        )
    _assert_only_fence_refusal(harness, before, verb="record_routing_event", membership=True)


def test_f10_fenced_verb_inventory_has_retained_stale_refusal_coverage() -> None:
    """No new fenced mutation surface may escape the F-10 cohort.

    The shared fence is complete-image tested above and the retained per-verb
    suites exercise the setup needed to make every payload write eligible.
    This architecture gate binds that evidence to the complete live caller
    inventory so adding a fenced verb requires adding stale-authority evidence.

    All three authority scopes are explicit: leader transactions (including
    ``fenced_write``), member transactions (including the heartbeat helper),
    and claimed-item transactions. Each scope has its own exact verb set and
    requires its own refusal exception; a member refusal cannot stand in for
    evidence that a stale item claim is rejected.

    Each verb declares HOW its refusal is evidenced, because not every fenced
    verb propagates. A verb called from a ``finally`` arm reifies the refusal
    instead: re-raising there would mask the exception being unwound. For
    those, the evidence is the durable ``fence_refusal`` row plus zero
    mutation, read back through a ``_fence_refusals`` helper — the same proof,
    one layer down. Every other verb must still assert the exception.
    """
    expected = {
        "allocate_call_index",
        "fork_token",
        "record_call",
        "record_call_payload_refs",
        "record_routing_events",
        "record_token_outcome",
        "record_transform_error",
        "_transition",
        "_transition_with_ready_children",
        "allocate_operation_call_index",
        "begin_node_state",
        "begin_node_states_many",
        "begin_operation",
        "claim_pending_sink",
        "claim_ready",
        "coalesce_tokens",
        "collect_tokens",
        "complete_aggregation_result",
        "complete_batch",
        "complete_node_state",
        "complete_operation",
        "create_batch",
        "create_token",
        "enqueue_ready",
        "enqueue_ready_claimed",
        "expand_token",
        "finalize_coalesce_effect",
        "record_completed_node_state",
        "record_empty_expansion",
        "record_operation_call",
        "record_operation_call_payload_refs",
        "record_routing_event",
        "record_token_outcome_leader",
        "record_validation_error",
        "register_edge",
        "register_node",
        "retry_batch",
        "update_batch_status",
        "update_grade_after_purge",
        "update_node_output_contract",
        "depart_worker",
        "release_seat",
        "worker_heartbeat",
        "acquire_lease",
        "adopt_blocked_barrier_item",
        "adopt_group_losses",
        "begin_attempt",
        "claim_preparation",
        "complete_barrier",
        "complete_member_result",
        "complete_plan",
        "complete_run",
        "create_checkpoint",
        "create_row_with_token",
        "delete_checkpoints",
        "evict_worker",
        "finalize",
        "heartbeat_lease",
        "ingest_row_with_initial_claim",
        "mark_pending_sink_terminal",
        "mark_pending_sink_terminal_many",
        "mark_response_lost",
        "reconcile_source_completions_from_scheduler",
        "record_attempt_result",
        "record_preflight_results",
        "record_readiness_check",
        "record_run_source",
        "record_secret_resolutions",
        "record_source_field_resolution",
        "recover_expired_leases",
        "register_candidate",
        "register_verified_candidate",
        "reserve",
        "reset_adoption_marker_to_pending",
        "set_export_failed_unless_completed",
        "set_export_pending_unless_completed",
        "set_export_status",
        "stage_escalation_loss",
        "takeover_expired",
        "terminalize_pending_sinks_with_terminal_outcomes",
        "update_run_source_contract",
        "update_run_status",
        "web_terminal_reconciliation",
        "run-start-reset-prepared",
        "run-start-effects",
    }
    member_verbs = {
        "update_node_output_contract",
        "expand_token",
        "record_empty_expansion",
        "begin_node_state",
        "complete_node_state",
        "record_routing_event",
        "worker_heartbeat",
        "depart_worker",
        "record_readiness_check",
        "_transition",
        "_transition_with_ready_children",
        "claim_ready",
        "heartbeat_lease",
        "enqueue_ready",
        "enqueue_ready_claimed",
    }
    item_verbs = {
        "fork_token",
        "allocate_call_index",
        "record_call_payload_refs",
        "record_call",
        "record_token_outcome",
        "record_routing_events",
        "record_transform_error",
    }
    actual_items: set[str] = set()
    actual: set[str] = set()
    actual_members: set[str] = set()
    for parsed in iter_gate_sources(ROOT / "src/elspeth"):
        for node in ast.walk(parsed.tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if function_name not in {
                "fenced_leader_transaction",
                "fenced_member_transaction",
                "fenced_heartbeat_transaction",
                "fenced_item_transaction",
                "fenced_write",
                "_fenced_or_plain_write",
            }:
                continue
            for keyword in node.keywords:
                if keyword.arg == "verb" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    actual.add(keyword.value.value)
                    if function_name in {"fenced_member_transaction", "fenced_heartbeat_transaction"}:
                        actual_members.add(keyword.value.value)
                    if function_name == "fenced_item_transaction":
                        actual_items.add(keyword.value.value)
    assert actual == expected
    assert actual_members == member_verbs
    assert actual_items == item_verbs

    retained_tests = {
        "web_terminal_reconciliation": (
            "tests/unit/web/execution/test_recovery_coordinator.py",
            "test_stale_reconciliation_leader_refuses_before_projection",
        ),
        "run-start-reset-prepared": (
            "tests/unit/core/landscape/test_run_start_admission.py",
            "test_stale_leader_cannot_change_start_admission_or_setup",
        ),
        "run-start-effects": (
            "tests/unit/core/landscape/test_run_start_admission.py",
            "test_stale_leader_cannot_change_start_admission_or_setup",
        ),
        "allocate_call_index": ("tests/unit/core/landscape/test_execution_authority.py", "test_stale_item_cannot_allocate_or_record_calls"),
        "fork_token": ("tests/unit/core/landscape/test_data_flow_fencing.py", "test_reclaimed_item_refuses_without_payload_mutation"),
        "record_call": ("tests/unit/core/landscape/test_execution_authority.py", "test_stale_item_cannot_allocate_or_record_calls"),
        "record_call_payload_refs": (
            "tests/unit/core/landscape/test_call_recording.py",
            "test_f10_call_payload_refs_refuse_reclaimed_item",
        ),
        "record_routing_events": (
            "tests/unit/core/landscape/test_node_state_recording.py",
            "test_f10_routing_events_refuse_reclaimed_item",
        ),
        "record_token_outcome": (
            "tests/unit/core/landscape/test_data_flow_fencing.py",
            "test_reclaimed_item_refuses_without_payload_mutation",
        ),
        "record_transform_error": (
            "tests/unit/core/landscape/test_data_flow_fencing.py",
            "test_reclaimed_item_refuses_without_payload_mutation",
        ),
        "_transition": (
            "tests/unit/core/landscape/test_state_engine_forbidden_paths.py",
            "test_f10_evicted_member_cannot_disposition_owned_work",
        ),
        "_transition_with_ready_children": (
            "tests/unit/core/landscape/test_state_engine_forbidden_paths.py",
            "test_f10_evicted_member_cannot_disposition_owned_work",
        ),
        "allocate_operation_call_index": (
            "tests/unit/core/landscape/test_execution_authority.py",
            "test_stale_leader_cannot_mutate_operation",
        ),
        "begin_node_state": (
            "tests/unit/core/landscape/test_execution_authority.py",
            "test_departed_member_cannot_begin_or_complete_state",
        ),
        "begin_node_states_many": (
            "tests/unit/core/landscape/test_execution_authority.py",
            "test_stale_leader_cannot_create_execution_records",
        ),
        "begin_operation": ("tests/unit/core/landscape/test_execution_authority.py", "test_stale_leader_cannot_create_execution_records"),
        "claim_pending_sink": (
            "tests/unit/core/landscape/test_state_engine_forbidden_paths.py",
            "test_f06_inactive_registered_worker_cannot_claim",
        ),
        "claim_ready": (
            "tests/unit/core/landscape/test_state_engine_forbidden_paths.py",
            "test_f06_inactive_registered_worker_cannot_claim",
        ),
        "coalesce_tokens": ("tests/unit/core/landscape/test_data_flow_fencing.py", "test_coalesce_materialization_is_leader_fenced"),
        "collect_tokens": ("tests/unit/core/landscape/test_data_flow_fencing.py", "test_collector_release_is_leader_fenced"),
        "complete_aggregation_result": (
            "tests/unit/core/landscape/test_state_engine_forbidden_paths.py",
            "test_f10_aggregation_result_requires_current_leader",
        ),
        "complete_batch": ("tests/unit/core/landscape/test_execution_authority.py", "test_stale_leader_cannot_mutate_batch"),
        "complete_node_state": (
            "tests/unit/core/landscape/test_execution_authority.py",
            "test_departed_member_cannot_begin_or_complete_state",
        ),
        "complete_operation": ("tests/unit/core/landscape/test_execution_authority.py", "test_stale_leader_cannot_mutate_operation"),
        "create_batch": ("tests/unit/core/landscape/test_execution_authority.py", "test_stale_leader_cannot_create_execution_records"),
        "create_token": ("tests/unit/core/landscape/test_data_flow_fencing.py", "test_stale_epoch_refuses_without_payload_mutation"),
        "enqueue_ready": ("tests/unit/core/landscape/test_finalize_follower_departure.py", "test_evicted_leader_raises"),
        "enqueue_ready_claimed": (
            "tests/unit/core/landscape/test_coordination_fence_constructs.py",
            "test_absent_or_evicted_identity_is_refused_with_full_zero_mutation",
        ),
        "expand_token": ("tests/unit/core/landscape/test_data_flow_fencing.py", "test_evicted_member_refuses_without_payload_mutation"),
        "finalize_coalesce_effect": ("tests/unit/core/landscape/test_data_flow_fencing.py", "test_coalesce_finalization_is_leader_fenced"),
        "record_completed_node_state": (
            "tests/unit/core/landscape/test_execution_authority.py",
            "test_stale_leader_cannot_create_execution_records",
        ),
        "record_empty_expansion": (
            "tests/unit/core/landscape/test_data_flow_fencing.py",
            "test_evicted_member_refuses_without_payload_mutation",
        ),
        "record_operation_call": ("tests/unit/core/landscape/test_execution_authority.py", "test_stale_leader_cannot_mutate_operation"),
        "record_operation_call_payload_refs": (
            "tests/unit/core/landscape/test_execution_authority.py",
            "test_operation_call_rechecks_authority_after_payload_storage",
        ),
        "record_routing_event": (
            "tests/unit/core/landscape/test_state_engine_forbidden_paths.py",
            "test_f10_evicted_member_cannot_record_routing_event",
        ),
        "record_token_outcome_leader": (
            "tests/unit/core/landscape/test_data_flow_fencing.py",
            "test_stale_epoch_refuses_without_payload_mutation",
        ),
        "record_validation_error": (
            "tests/unit/core/landscape/test_data_flow_fencing.py",
            "test_stale_epoch_refuses_without_payload_mutation",
        ),
        "register_edge": ("tests/unit/core/landscape/test_data_flow_fencing.py", "test_stale_epoch_refuses_without_payload_mutation"),
        "register_node": ("tests/unit/core/landscape/test_data_flow_fencing.py", "test_stale_epoch_refuses_without_payload_mutation"),
        "retry_batch": ("tests/unit/core/landscape/test_execution_authority.py", "test_stale_leader_cannot_mutate_batch"),
        "update_batch_status": ("tests/unit/core/landscape/test_execution_authority.py", "test_stale_leader_cannot_mutate_batch"),
        "update_grade_after_purge": (
            "tests/unit/core/landscape/test_reproducibility.py",
            "test_purge_grade_requires_current_export_authority",
        ),
        "update_node_output_contract": (
            "tests/unit/core/landscape/test_data_flow_fencing.py",
            "test_evicted_member_refuses_without_payload_mutation",
        ),
        "adopt_blocked_barrier_item": (
            "tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py",
            "test_stale_token_refused_with_fence_refusal_and_zero_mutation",
        ),
        "adopt_group_losses": (
            "tests/e2e/recovery/test_suspended_winner_fences.py",
            "test_stale_adopt_group_losses_refused",
        ),
        "complete_barrier": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_complete_barrier_strict_arm_refused",
        ),
        "complete_run": ("tests/unit/core/landscape/test_leader_fence_stale_token.py", "test_complete_run_refused"),
        "create_checkpoint": ("tests/unit/core/landscape/test_leader_fence_stale_token.py", "test_create_checkpoint_refused"),
        "create_row_with_token": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_fenced_create_row_with_token_refused",
        ),
        "delete_checkpoints": ("tests/unit/core/landscape/test_leader_fence_stale_token.py", "test_delete_checkpoints_refused"),
        "evict_worker": (
            "tests/unit/core/landscape/test_run_coordination_repository.py",
            "test_evict_worker_grace_predicate_and_fence",
        ),
        "ingest_row_with_initial_claim": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_ingest_woken_mid_ingest_atomic_rollback",
        ),
        "mark_pending_sink_terminal": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_mark_pending_sink_terminal_refused",
        ),
        "mark_pending_sink_terminal_many": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_mark_pending_sink_terminal_many_refused",
        ),
        "reconcile_source_completions_from_scheduler": (
            "tests/unit/core/landscape/test_state_engine_forbidden_paths.py",
            "test_f10_stale_leader_cannot_reconcile_source_completions",
        ),
        "recover_expired_leases": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_recover_expired_leases_refused",
        ),
        # The audit-export registry CAS (ADR-048): its stale-token evidence
        # lives beside the export-bundle derivation machinery it needs to
        # build a verified candidate, not in the shared fence suite. Both
        # public write verbs open their own fence, so both owe evidence.
        "register_candidate": (
            "tests/unit/core/landscape/test_audit_export_snapshots.py",
            "test_register_candidate_refused",
        ),
        "register_verified_candidate": (
            "tests/unit/core/landscape/test_audit_export_snapshots.py",
            "test_register_verified_candidate_refused",
        ),
        "reset_adoption_marker_to_pending": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_reset_adoption_marker_to_pending_refused",
        ),
        "stage_escalation_loss": (
            "tests/e2e/recovery/test_suspended_winner_fences.py",
            "test_stale_stage_escalation_loss_refused",
        ),
        "terminalize_pending_sinks_with_terminal_outcomes": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_terminalize_pending_sinks_refused",
        ),
        "update_run_status": (
            "tests/unit/core/landscape/test_state_engine_forbidden_paths.py",
            "test_f10_stale_leader_refusal_adds_only_fence_refusal_event",
        ),
        "record_preflight_results": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_run_lifecycle_verb_refused",
        ),
        "record_readiness_check": (
            "tests/unit/core/landscape/test_state_engine_forbidden_paths.py",
            "test_f10_readiness_refuses_lost_membership",
        ),
        "record_run_source": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_run_lifecycle_verb_refused",
        ),
        "record_secret_resolutions": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_run_lifecycle_verb_refused",
        ),
        "record_source_field_resolution": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_run_lifecycle_verb_refused",
        ),
        "set_export_failed_unless_completed": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_run_lifecycle_verb_refused",
        ),
        "set_export_pending_unless_completed": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_run_lifecycle_verb_refused",
        ),
        "set_export_status": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_run_lifecycle_verb_refused",
        ),
        "update_run_source_contract": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_run_lifecycle_verb_refused",
        ),
        "release_seat": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_release_seat_refused",
        ),
        "depart_worker": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_depart_worker_refused_for_departed_member",
        ),
        "worker_heartbeat": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_worker_heartbeat_refused_for_evicted_member",
        ),
        # D8.5 sink-effect family (ADR-048): nine verbs share one parametrized
        # arm; `reserve` and `finalize` need a real effect to reach the fence
        # (both validate a witness before the write transaction opens), so
        # each has its own.
        "acquire_lease": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_sink_effect_verb_refused",
        ),
        "begin_attempt": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_sink_effect_verb_refused",
        ),
        "claim_preparation": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_sink_effect_verb_refused",
        ),
        "complete_member_result": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_sink_effect_verb_refused",
        ),
        "complete_plan": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_sink_effect_verb_refused",
        ),
        "heartbeat_lease": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_sink_effect_verb_refused",
        ),
        "mark_response_lost": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_sink_effect_verb_refused",
        ),
        "record_attempt_result": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_sink_effect_verb_refused",
        ),
        "takeover_expired": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_sink_effect_verb_refused",
        ),
        "reserve": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_sink_effect_reserve_refused",
        ),
        "finalize": (
            "tests/unit/core/landscape/test_leader_fence_stale_token.py",
            "test_sink_effect_finalize_refused",
        ),
    }
    # Verbs whose refusal is REIFIED rather than propagated (see the
    # docstring): each is called from a teardown or liveness arm where
    # re-raising would mask the exception being unwound, so its evidence is
    # the durable fence_refusal row rather than the exception.
    reified_refusal_verbs = {"depart_worker", "release_seat", "worker_heartbeat"}
    assert reified_refusal_verbs <= expected
    assert set(retained_tests) == expected
    for verb, (relative_path, test_name) in retained_tests.items():
        test_path = ROOT / relative_path
        test_tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
        matches = [
            node for node in ast.walk(test_tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == test_name
        ]
        assert len(matches) == 1, f"F-10 verb {verb!r} must bind one retained test function {test_name!r}"
        test_source = ast.unparse(matches[0])
        local_functions = {node.name: node for node in ast.walk(test_tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        named_matrices = {
            target.id: node.value
            for node in test_tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and isinstance(node.value, (ast.List, ast.Tuple))
        }
        reachable_calls: set[str] = set()
        pending: list[ast.AST] = [matches[0]]
        visited: set[int] = set()
        while pending:
            function = pending.pop()
            if id(function) in visited:
                continue
            visited.add(id(function))
            for reference in (node for node in ast.walk(function) if isinstance(node, ast.Name)):
                if reference.id in local_functions:
                    pending.append(local_functions[reference.id])
                if reference.id in named_matrices:
                    pending.append(named_matrices[reference.id])
            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                call_name = (
                    call.func.id if isinstance(call.func, ast.Name) else call.func.attr if isinstance(call.func, ast.Attribute) else None
                )
                if call_name is None:
                    continue
                reachable_calls.add(call_name)
                if call_name in local_functions:
                    pending.append(local_functions[call_name])
        source_entry_points = {
            "web_terminal_reconciliation": (
                "src/elspeth/web/execution/recovery.py",
                "_reconcile_resumable_terminal",
            ),
            "run-start-reset-prepared": (
                "src/elspeth/core/landscape/run_start_admission.py",
                "reset_prepared_initialization",
            ),
            "run-start-effects": (
                "src/elspeth/core/landscape/run_start_admission.py",
                "mark_executing",
            ),
            "_transition": ("src/elspeth/core/landscape/scheduler/dispositions.py", "mark_terminal"),
            "_transition_with_ready_children": (
                "src/elspeth/core/landscape/scheduler/dispositions.py",
                "mark_terminal_with_ready_children",
            ),
            "record_operation_call_payload_refs": ("src/elspeth/core/landscape/execution/calls.py", "record_operation_call"),
            "record_call_payload_refs": ("src/elspeth/core/landscape/execution/calls.py", "record_call"),
        }
        if verb in source_entry_points:
            source_path, entry = source_entry_points[verb]
            assert entry in reachable_calls
            source_tree = ast.parse((ROOT / source_path).read_text())
            methods = {node.name: node for node in ast.walk(source_tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            source_pending = [methods[entry]]
            source_seen: set[str] = set()
            source_verbs: set[str] = set()
            while source_pending:
                method = source_pending.pop()
                if method.name in source_seen:
                    continue
                source_seen.add(method.name)
                for call in (node for node in ast.walk(method) if isinstance(node, ast.Call)):
                    if (
                        isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "self"
                        and call.func.attr in methods
                    ):
                        source_pending.append(methods[call.func.attr])
                    for keyword in call.keywords:
                        if keyword.arg == "verb" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                            source_verbs.add(keyword.value.value)
            assert verb in source_verbs, f"{entry} no longer reaches the source fence for {verb}"
        else:
            assert verb in reachable_calls, f"F-10 test {test_name!r} no longer invokes {verb!r}, directly or through a local helper"
        if verb in reified_refusal_verbs:
            assert "_fence_refusals" in reachable_calls, (
                f"F-10 test {test_name!r} no longer reads back {verb!r}'s durable fence_refusal evidence; "
                "a verb that swallows its refusal has no other proof the fence fired"
            )
        else:
            if verb in item_verbs:
                error = "SchedulerLeaseLostError"
            elif verb in member_verbs and verb != "heartbeat_lease":
                error = "RunMembershipLostError"
            else:
                error = "RunLeadershipLostError"
            if verb == "enqueue_ready_claimed":
                # This matrix also covers absent registration (AuditIntegrityError).
                assert "AuditIntegrityError if caller_status is None else RunMembershipLostError" in test_source
                assert "pytest.raises(expected_error)" in test_source
            else:
                assert f"pytest.raises({error})" in test_source, f"F-10 test {test_name!r} no longer asserts {error}"
    # The shared heartbeat verb label has a leader-scoped sink-effect writer
    # and a member-scoped scheduler writer; retain independent refusal proof.
    heartbeat_tree = ast.parse((ROOT / "tests/unit/core/landscape/test_coordination_fence_constructs.py").read_text())
    heartbeat = next(
        node
        for node in ast.walk(heartbeat_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "test_non_active_owner_is_refused_with_zero_durable_mutation"
    )
    assert "pytest.raises(RunMembershipLostError)" in ast.unparse(heartbeat)
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "heartbeat_lease"
        for node in ast.walk(heartbeat)
    )


def test_f12_waiting_state_is_rejected_by_storage_without_mutation(harness: _Harness) -> None:
    _row_id, _token_id, work_item_id = _enqueue(harness, "waiting", 0)
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)

    with pytest.raises(IntegrityError, match="ck_token_work_items_status"), harness.db.engine.begin() as conn:
        conn.execute(update(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id).values(status="waiting"))

    assert capture_state_engine_image(harness.db, run_id=RUN_ID) == before
    assert ("token_work_items", "ck_token_work_items_status") in database_module._REQUIRED_CHECK_CONSTRAINTS


def test_f12_waiting_is_unreachable_from_scheduler_and_restore_code() -> None:
    assert {status.value for status in TokenWorkStatus} == {
        "ready",
        "leased",
        "blocked",
        "pending_sink",
        "terminal",
        "failed",
    }
    production_paths = (
        ROOT / "src/elspeth/core/landscape/scheduler",
        ROOT / "src/elspeth/core/landscape/scheduler_repository.py",
        ROOT / "src/elspeth/engine/orchestrator/resume.py",
    )
    offenders: list[str] = []
    for path in production_paths:
        trees = (
            ((parsed.path, parsed.tree) for parsed in iter_gate_sources(path))
            if path.is_dir()
            else [(path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))]
        )
        for file_path, tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "waiting":
                    offenders.append(f"{file_path.relative_to(ROOT)}:{node.lineno}:string")
                if isinstance(node, ast.Attribute) and node.attr == "WAITING":
                    offenders.append(f"{file_path.relative_to(ROOT)}:{node.lineno}:attribute")
    assert offenders == []


@pytest.mark.parametrize("stale", [False, True], ids=["current-leader", "stale-leader"])
def test_f10_aggregation_result_requires_current_leader(harness: _Harness, stale: bool) -> None:
    register_test_node(harness.factory.data_flow, RUN_ID, "aggregation-1", node_type=NodeType.AGGREGATION)
    _, token_id, _ = _enqueue(harness, "aggregation-member", 0)
    execution = harness.factory.execution
    state = execution.begin_node_state(token_id, "aggregation-1", 1, {"value": 1}, member_token=harness.coordination_token.membership)
    batch = execution.create_batch("aggregation-1", coordination_token=harness.coordination_token)
    with fenced_leader_transaction(
        harness.db.engine, token=harness.coordination_token, window_seconds=300, verb="test_aggregation_receipt_setup"
    ) as conn:
        add_batch_member_guarded(conn, batch_id=batch.batch_id, token_id=token_id, ordinal=0, expected_run_id=RUN_ID)
        record_buffered_outcome_guarded(conn, run_id=RUN_ID, token_id=token_id, batch_id=batch.batch_id, recorded_at=datetime.now(UTC))
    execution.update_batch_status(
        batch.batch_id, BatchStatus.EXECUTING, state_id=state.state_id, coordination_token=harness.coordination_token
    )
    context = AggregationFlushContext(
        trigger_type=TriggerType.END_OF_SOURCE.value,
        buffer_size=1,
        batch_id=batch.batch_id,
        flush_index=1,
        rows_seen_total=1,
        row_start=1,
        row_end=1,
        is_end_of_source=True,
    )
    members = (AggregationResultMember(TokenRef(token_id=token_id, run_id=RUN_ID), AggregationMemberAction.DROP_FILTERED),)
    if stale:
        before = _depose_leader(harness)
        with pytest.raises(RunLeadershipLostError) as raised:
            execution.complete_aggregation_result(
                batch_id=batch.batch_id,
                coordination_token=harness.coordination_token,
                aggregation_node_id="aggregation-1",
                state_id=state.state_id,
                trigger_type=TriggerType.END_OF_SOURCE,
                output_mode=OutputMode.TRANSFORM,
                output_rows=(),
                output_shape="empty",
                output_hash=stable_hash([]),
                members=members,
                expansion_parent_token_id=None,
                duration_ms=1.0,
                success_reason=None,
                context_after=context,
            )
        assert raised.value.verb == "complete_aggregation_result"
        _assert_only_fence_refusal(harness, before, verb="complete_aggregation_result")
    else:
        receipt = execution.complete_aggregation_result(
            batch_id=batch.batch_id,
            coordination_token=harness.coordination_token,
            aggregation_node_id="aggregation-1",
            state_id=state.state_id,
            trigger_type=TriggerType.END_OF_SOURCE,
            output_mode=OutputMode.TRANSFORM,
            output_rows=(),
            output_shape="empty",
            output_hash=stable_hash([]),
            members=members,
            expansion_parent_token_id=None,
            duration_ms=1.0,
            success_reason=None,
            context_after=context,
        )
        assert receipt.batch_id == batch.batch_id
        assert receipt.members == members
        completed_state = execution.get_node_state(state.state_id)
        completed_batch = execution.get_batch(batch.batch_id)
        assert completed_state is not None and completed_state.status is NodeStateStatus.COMPLETED
        assert completed_batch is not None and completed_batch.status is BatchStatus.COMPLETED


@pytest.mark.parametrize("status", ["departed", "evicted"])
def test_f10_readiness_refuses_lost_membership(harness: _Harness, status: str) -> None:
    member = harness.coordination_token.membership
    with harness.db.engine.begin() as conn:
        conn.execute(
            update(run_workers_table)
            .where(run_workers_table.c.worker_id == member.worker_id)
            .values(status=status, evicted_at=datetime.now(UTC) if status == "evicted" else None)
        )
    before = capture_state_engine_image(harness.db, run_id=RUN_ID)
    with pytest.raises(RunMembershipLostError) as raised:
        harness.factory.run_lifecycle.record_readiness_check(
            member_token=member,
            name="search",
            collection="documents",
            reachable=True,
            count=2,
            message="ready",
        )
    assert raised.value.verb == "record_readiness_check"
    _assert_only_fence_refusal(harness, before, verb="record_readiness_check", membership=True)
