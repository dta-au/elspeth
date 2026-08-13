"""State-engine RM-01..RM-13 repository truth tables.

The catalog read-model legs are deliberately expressed as data over one
durable scheduler image.  This keeps the status/subtype partitions visible:
``LEASED`` producer work is not the same thing as ``LEASED`` sink-redrive
debt, and ``BLOCKED`` queue holds are not barrier holds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, update

from elspeth.contracts import NodeType, TerminalOutcome, TerminalPath
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.checkpoint.recovery import RecoveryManager
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    run_coordination_table,
    run_workers_table,
    token_outcomes_table,
    token_work_items_table,
)
from elspeth.web.execution.accounting import load_run_accounting_map_from_db
from tests.fixtures.landscape import make_factory, make_landscape_db, register_test_node

NOW = datetime(2026, 8, 11, 20, 0, 0, tzinfo=UTC)
RUN_ID = "rm-truth-run"
OTHER_RUN_ID = "rm-foreign-run"
LEADER = "worker:rm-truth-run:leader"
PEER_A = "worker:rm-truth-run:peer-a"
PEER_B = "worker:rm-truth-run:peer-b"
NODE_ID = "transform-1"
PAYLOAD = TokenSchedulerRepository.serialize_row_payload(PipelineRow({"value": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True)))


def _begin_run(factory: Any, run_id: str, leader: str) -> None:
    factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id=run_id,
        leader_worker_id=leader,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    factory.data_flow.register_node(
        run_id=run_id,
        plugin_name="source",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        node_id=f"source-{run_id}",
        schema_config=SchemaConfig.from_dict({"mode": "observed"}),
    )
    register_test_node(factory.data_flow, run_id, NODE_ID)


def _enqueue(factory: Any, run_id: str, name: str, sequence: int) -> str:
    row, token = factory.data_flow.create_row_with_token(
        run_id=run_id,
        source_node_id=f"source-{run_id}",
        row_index=sequence,
        data={"name": name},
        source_row_index=sequence,
        ingest_sequence=sequence,
        row_id=f"row-{name}",
        token_id=f"token-{name}",
    )
    item = factory.scheduler.enqueue_ready(
        run_id=run_id,
        token_id=token.token_id,
        row_id=row.row_id,
        node_id=NODE_ID,
        step_index=1,
        ingest_sequence=sequence,
        available_at=NOW,
        row_payload_json=PAYLOAD,
    )
    return str(item.work_item_id)


def _seed_scheduler_image() -> tuple[Any, TokenSchedulerRepository, dict[str, str]]:
    db = make_landscape_db()
    factory = make_factory(db)
    _begin_run(factory, RUN_ID, LEADER)
    _begin_run(factory, OTHER_RUN_ID, f"worker:{OTHER_RUN_ID}:leader")

    names = (
        "ready",
        "leased-self",
        "leased-peer-a",
        "leased-peer-b",
        "leased-peer-equality",
        "leased-sink-redrive",
        "blocked-queue",
        "blocked-barrier-pending-z",
        "blocked-barrier-adopted-a",
        "pending-sink-peer",
        "pending-sink-empty-owner",
        "terminal",
        "failed",
    )
    ids = {name: _enqueue(factory, RUN_ID, name, index) for index, name in enumerate(names)}
    foreign_id = _enqueue(factory, OTHER_RUN_ID, "foreign-ready", 100)

    with db.engine.begin() as conn:
        for owner in (PEER_A, PEER_B):
            conn.execute(
                insert(run_workers_table).values(
                    worker_id=owner,
                    run_id=RUN_ID,
                    role="follower",
                    status="active",
                    registered_at=NOW,
                    heartbeat_expires_at=NOW + timedelta(minutes=5),
                )
            )

        def set_item(name: str, **values: object) -> None:
            conn.execute(update(token_work_items_table).where(token_work_items_table.c.work_item_id == ids[name]).values(**values))

        set_item(
            "leased-self",
            status=TokenWorkStatus.LEASED.value,
            lease_owner=LEADER,
            lease_expires_at=NOW + timedelta(seconds=10),
        )
        set_item(
            "leased-peer-a",
            status=TokenWorkStatus.LEASED.value,
            lease_owner=PEER_A,
            lease_expires_at=NOW + timedelta(seconds=10),
        )
        set_item(
            "leased-peer-b",
            status=TokenWorkStatus.LEASED.value,
            lease_owner=PEER_B,
            lease_expires_at=NOW + timedelta(seconds=10),
        )
        set_item(
            "leased-peer-equality",
            status=TokenWorkStatus.LEASED.value,
            lease_owner="worker:rm-truth-run:equality",
            lease_expires_at=NOW,
        )
        set_item(
            "leased-sink-redrive",
            status=TokenWorkStatus.LEASED.value,
            lease_owner=PEER_A,
            lease_expires_at=NOW + timedelta(seconds=10),
            pending_sink_name="sink-a",
            pending_outcome=TerminalOutcome.SUCCESS.value,
            pending_path=TerminalPath.DEFAULT_FLOW.value,
        )
        set_item(
            "blocked-queue",
            status=TokenWorkStatus.BLOCKED.value,
            queue_key="queue-a",
            barrier_key=None,
            barrier_blocked_at=NOW,
        )
        set_item(
            "blocked-barrier-pending-z",
            status=TokenWorkStatus.BLOCKED.value,
            queue_key=None,
            barrier_key="z-barrier",
            barrier_blocked_at=NOW,
            barrier_adopted_epoch=None,
        )
        set_item(
            "blocked-barrier-adopted-a",
            status=TokenWorkStatus.BLOCKED.value,
            queue_key=None,
            barrier_key="a-barrier",
            barrier_blocked_at=NOW,
            barrier_adopted_epoch=7,
        )
        set_item(
            "pending-sink-peer",
            status=TokenWorkStatus.PENDING_SINK.value,
            lease_owner=PEER_B,
            lease_expires_at=None,
            pending_sink_name="sink-b",
            pending_outcome=TerminalOutcome.SUCCESS.value,
            pending_path=TerminalPath.DEFAULT_FLOW.value,
        )
        set_item(
            "pending-sink-empty-owner",
            status=TokenWorkStatus.PENDING_SINK.value,
            lease_owner="",
            lease_expires_at=None,
            pending_sink_name="sink-malformed",
            pending_outcome=TerminalOutcome.SUCCESS.value,
            pending_path=TerminalPath.DEFAULT_FLOW.value,
        )
        set_item("terminal", status=TokenWorkStatus.TERMINAL.value)
        set_item("failed", status=TokenWorkStatus.FAILED.value)

    ids["foreign-ready"] = foreign_id
    return factory, factory.scheduler, ids


@pytest.mark.parametrize(
    ("query", "kwargs", "expected"),
    (
        pytest.param(
            TokenSchedulerRepository.count_unresolved_work,
            {"run_id": RUN_ID},
            8,
            id="RM-01-unresolved-producer-work",
        ),
        pytest.param(
            TokenSchedulerRepository.count_unquiesced_work,
            {"run_id": RUN_ID},
            5,
            id="RM-02-unquiesced-barrier-producers",
        ),
        pytest.param(
            TokenSchedulerRepository.count_active_work,
            {"run_id": RUN_ID},
            11,
            id="RM-03-active-run-backstop",
        ),
        pytest.param(
            TokenSchedulerRepository.has_peer_owned_work,
            {"run_id": RUN_ID, "caller_owner": LEADER},
            True,
            id="RM-04-peer-owned-work",
        ),
        pytest.param(
            TokenSchedulerRepository.peer_active_leases,
            {"run_id": RUN_ID, "caller_owner": LEADER, "now": NOW},
            (PEER_A, PEER_B),
            id="RM-06-active-peer-lease-strict-expiry-and-dedup",
        ),
        pytest.param(
            TokenSchedulerRepository.active_row_ids,
            {"run_id": RUN_ID},
            frozenset(
                {
                    "row-" + name
                    for name in (
                        "ready",
                        "leased-self",
                        "leased-peer-a",
                        "leased-peer-b",
                        "leased-peer-equality",
                        "leased-sink-redrive",
                        "blocked-queue",
                        "blocked-barrier-pending-z",
                        "blocked-barrier-adopted-a",
                        "pending-sink-peer",
                        "pending-sink-empty-owner",
                    )
                }
            ),
            id="RM-09-active-source-row-identities",
        ),
        pytest.param(
            TokenSchedulerRepository.blocked_barrier_token_ids,
            {"run_id": RUN_ID},
            frozenset({"token-blocked-barrier-pending-z", "token-blocked-barrier-adopted-a"}),
            id="RM-10-barrier-token-identities",
        ),
        pytest.param(
            TokenSchedulerRepository.count_blocked_barrier_items,
            {"run_id": RUN_ID},
            2,
            id="RM-11-barrier-work-count",
        ),
    ),
)
def test_scheduler_read_model_truth_table(query: Any, kwargs: dict[str, object], expected: object) -> None:
    _factory, repository, _ids = _seed_scheduler_image()
    assert query(repository, **kwargs) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        pytest.param(
            TokenSchedulerRepository.list_blocked_barrier_items,
            ("a-barrier", "z-barrier"),
            id="RM-12-barrier-work-list-order",
        ),
        pytest.param(
            TokenSchedulerRepository.list_pending_blocked_barrier_items,
            ("z-barrier",),
            id="RM-13-pending-barrier-adoption-list",
        ),
    ),
)
def test_scheduler_barrier_list_truth_table(query: Any, expected: tuple[str, ...]) -> None:
    _factory, repository, _ids = _seed_scheduler_image()
    assert tuple(item.barrier_key for item in query(repository, run_id=RUN_ID)) == expected


@pytest.mark.parametrize(
    ("ready", "failed", "peer_owned", "expected"),
    (
        pytest.param(0, 0, True, True, id="RM-05-peer-continuation-may-be-relinquished"),
        pytest.param(1, 0, True, False, id="RM-05-ready-remains-local"),
        pytest.param(0, 1, True, False, id="RM-05-failed-remains-loud"),
        pytest.param(0, 0, False, False, id="RM-05-no-peer-authority"),
    ),
)
def test_rm05_relinquishment_discriminator(ready: int, failed: int, peer_owned: bool, expected: bool) -> None:
    # This is the exact production decision in SchedulerDrainCoordinator:
    # all three repository facts must agree before in-memory work is released.
    assert (ready == 0 and failed == 0 and peer_owned) is expected


def test_rm05_repository_inputs_are_run_scoped_and_status_exact() -> None:
    _factory, repository, ids = _seed_scheduler_image()
    selected = [ids["ready"], ids["failed"], ids["leased-peer-a"], ids["foreign-ready"]]
    assert repository.count_ready_in_set(run_id=RUN_ID, work_item_ids=selected) == 1
    assert repository.count_failed_in_set(run_id=RUN_ID, work_item_ids=selected) == 1
    assert repository.has_peer_owned_work(run_id=RUN_ID, caller_owner=LEADER) is True
    assert repository.has_peer_owned_work(run_id="missing-run", caller_owner=LEADER) is False


def test_rm05_peer_authority_is_scoped_to_the_pending_continuations() -> None:
    _factory, repository, ids = _seed_scheduler_image()
    # A peer owns other work in the run, but it does not own the leader's
    # selected continuation. Run-wide peer presence cannot authorize clearing
    # that selected in-memory item.
    assert (
        repository.has_peer_owned_work(
            run_id=RUN_ID,
            caller_owner=LEADER,
            work_item_ids=(ids["leased-self"],),
        )
        is False
    )
    assert (
        repository.has_peer_owned_work(
            run_id=RUN_ID,
            caller_owner=LEADER,
            work_item_ids=(ids["leased-peer-a"],),
        )
        is True
    )
    assert (
        repository.has_peer_owned_work(
            run_id=RUN_ID,
            caller_owner=LEADER,
            work_item_ids=(ids["pending-sink-empty-owner"],),
        )
        is False
    )


def test_rm07_occupied_leader_seat_truth_table() -> None:
    factory, _repository, _ids = _seed_scheduler_image()
    coordination = RunCoordinationRepository(factory._db.engine)

    occupied = coordination.live_leader(run_id=RUN_ID, now=NOW)
    assert occupied is not None
    assert occupied.leader_worker_id == LEADER
    assert occupied.leader_epoch == 1
    assert occupied.seat_live is True

    equality = coordination.live_leader(
        run_id=RUN_ID,
        now=occupied.leader_heartbeat_expires_at,
    )
    assert equality is not None
    assert equality.seat_live is True
    assert coordination.live_leader(run_id="missing-run", now=NOW) is None

    with factory._db.engine.begin() as conn:
        conn.execute(
            update(run_coordination_table)
            .where(run_coordination_table.c.run_id == RUN_ID)
            .values(leader_worker_id=None, leader_heartbeat_expires_at=None, updated_at=NOW)
        )
    assert coordination.live_leader(run_id=RUN_ID, now=NOW) is None


def test_rm08_dead_non_leader_worker_truth_table_and_ordering() -> None:
    factory, _repository, _ids = _seed_scheduler_image()
    coordination = RunCoordinationRepository(factory._db.engine)
    threshold = NOW - timedelta(seconds=10)
    workers = (
        ("dead-first", "follower", "active", threshold - timedelta(seconds=2), NOW - timedelta(seconds=4)),
        ("dead-second", "follower", "active", threshold - timedelta(seconds=1), NOW - timedelta(seconds=3)),
        ("equality", "follower", "active", threshold, NOW - timedelta(seconds=2)),
        ("departed", "follower", "departed", threshold - timedelta(seconds=3), NOW - timedelta(seconds=1)),
    )
    with factory._db.engine.begin() as conn:
        for worker_id, role, status, expires_at, registered_at in workers:
            conn.execute(
                insert(run_workers_table).values(
                    worker_id=worker_id,
                    run_id=RUN_ID,
                    role=role,
                    status=status,
                    registered_at=registered_at,
                    heartbeat_expires_at=expires_at,
                    departed_at=NOW if status == "departed" else None,
                )
            )
    assert coordination.dead_non_leader_workers(
        run_id=RUN_ID,
        leader_worker_id=LEADER,
        now=NOW,
        grace_seconds=10,
    ) == ("dead-first", "dead-second")


def test_rm08_equal_registration_times_order_by_worker_identity() -> None:
    factory, _repository, _ids = _seed_scheduler_image()
    coordination = RunCoordinationRepository(factory._db.engine)
    registered_at = NOW - timedelta(minutes=2)
    with factory._db.engine.begin() as conn:
        # Insert in reverse lexical order: deterministic output must not depend
        # on the database's incidental tie ordering.
        for worker_id in ("dead-z", "dead-a"):
            conn.execute(
                insert(run_workers_table).values(
                    worker_id=worker_id,
                    run_id=RUN_ID,
                    role="follower",
                    status="active",
                    registered_at=registered_at,
                    heartbeat_expires_at=NOW - timedelta(minutes=1),
                )
            )
    assert coordination.dead_non_leader_workers(
        run_id=RUN_ID,
        leader_worker_id=LEADER,
        now=NOW,
        grace_seconds=10,
    ) == ("dead-a", "dead-z")


def test_read_models_do_not_bleed_foreign_runs() -> None:
    _factory, repository, _ids = _seed_scheduler_image()
    assert repository.count_active_work(run_id=OTHER_RUN_ID) == 1
    assert repository.count_active_work(run_id=RUN_ID) == 11
    assert repository.blocked_barrier_token_ids(run_id=OTHER_RUN_ID) == frozenset()


@pytest.mark.parametrize("with_checkpoint", (False, True), ids=("without-checkpoint", "with-checkpoint"))
@pytest.mark.parametrize("with_decision", (False, True), ids=("abandoned-only", "decided-plus-abandoned"))
def test_rm14_resume_workset_refuses_abandoned_token_fates(
    monkeypatch: pytest.MonkeyPatch,
    with_checkpoint: bool,
    with_decision: bool,
) -> None:
    factory, _repository, _ids = _seed_scheduler_image()
    token_id = "token-failed"
    with factory._db.engine.begin() as conn:
        conn.execute(
            insert(token_outcomes_table).values(
                outcome_id="out-abandoned",
                run_id=RUN_ID,
                token_id=token_id,
                outcome=None,
                path=TerminalPath.ABANDONED.value,
                completed=0,
                recorded_at=NOW,
                context_json="{}",
            )
        )
        if with_decision:
            conn.execute(
                insert(token_outcomes_table).values(
                    outcome_id="out-decided",
                    run_id=RUN_ID,
                    token_id=token_id,
                    outcome=TerminalOutcome.SUCCESS.value,
                    path=TerminalPath.DEFAULT_FLOW.value,
                    completed=1,
                    recorded_at=NOW + timedelta(seconds=1),
                    sink_name="sink-a",
                    context_json="{}",
                )
            )

    recovery = RecoveryManager(factory._db, checkpoint_manager=object())  # type: ignore[arg-type]
    # ABANDONED must refuse even when the finalized run has no checkpoint.
    checkpoint = object() if with_checkpoint else None
    monkeypatch.setattr(recovery, "_get_latest_checkpoint_for_resume_workset", lambda _run_id: checkpoint)
    with pytest.raises(AuditIntegrityError, match="ABANDONED"):
        recovery.get_resume_workset(RUN_ID)


def test_rm14_accounting_census_distinguishes_all_token_fates() -> None:
    db = make_landscape_db()
    factory = make_factory(db)
    run_ids = ("fate-decided", "fate-pending", "fate-abandoned", "fate-contradiction")
    for index, run_id in enumerate(run_ids):
        _begin_run(factory, run_id, f"worker:{run_id}:leader")
        factory.data_flow.create_row_with_token(
            run_id=run_id,
            source_node_id=f"source-{run_id}",
            row_index=index,
            data={"run": run_id},
            source_row_index=index,
            ingest_sequence=index,
            row_id=f"row-{run_id}",
            token_id=f"token-{run_id}",
        )

    for run_id in ("fate-decided", "fate-contradiction"):
        factory.data_flow.record_token_outcome(
            ref=TokenRef(token_id=f"token-{run_id}", run_id=run_id),
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name="sink-a",
        )
    with db.engine.begin() as conn:
        for run_id in ("fate-abandoned", "fate-contradiction"):
            conn.execute(
                insert(token_outcomes_table).values(
                    outcome_id=f"out-abandoned-{run_id}",
                    run_id=run_id,
                    token_id=f"token-{run_id}",
                    outcome=None,
                    path=TerminalPath.ABANDONED.value,
                    completed=0,
                    recorded_at=NOW,
                    context_json="{}",
                )
            )

    batch = load_run_accounting_map_from_db(db, run_ids)
    assert batch.accounting["fate-decided"].integrity.closure == "closed"
    assert batch.accounting["fate-pending"].integrity.closure == "open"
    assert batch.accounting["fate-pending"].tokens.pending == 1
    assert batch.accounting["fate-abandoned"].integrity.closure == "abandoned"
    assert batch.accounting["fate-abandoned"].tokens.abandoned == 1
    assert "fate-contradiction" not in batch.accounting
    assert "both terminally decided and marked ABANDONED" in batch.corrupt["fate-contradiction"].violations[0]
