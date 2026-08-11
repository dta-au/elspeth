"""Local PostgreSQL 16 support proofs for RM-01 through RM-14.

These tests exercise a real Testcontainers database.  They are deliberately
support-only PostgreSQL evidence: they do not use the state-engine profile
reporter and make no claim about a live AWS deployment composition.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
from tests.fixtures.landscape import make_factory, register_test_node

from elspeth.contracts import NodeType, TerminalOutcome, TerminalPath
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.checkpoint.recovery import RecoveryManager
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    run_coordination_table,
    run_workers_table,
    token_outcomes_table,
    token_work_items_table,
)
from elspeth.web.execution.accounting import load_run_accounting_map_from_db

pytestmark = [pytest.mark.testcontainer, pytest.mark.timeout(120)]

NOW = datetime(2026, 8, 11, 20, 0, tzinfo=UTC)
RUN_ID = "rm-postgresql-run"
OTHER_RUN_ID = "rm-postgresql-foreign-run"
LEADER = "worker:rm-postgresql-run:leader"
PEER_A = "worker:rm-postgresql-run:peer-a"
PEER_B = "worker:rm-postgresql-run:peer-b"
NODE_ID = "transform-postgresql"
PAYLOAD = TokenSchedulerRepository.serialize_row_payload(PipelineRow({"value": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True)))


@pytest.fixture(scope="module")
def postgres_db() -> Iterator[LandscapeDB]:
    """A real local PostgreSQL 16 backend, not an AWS profile surrogate."""
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        db = LandscapeDB.from_url(postgres.get_connection_url())
        try:
            yield db
        finally:
            db.close()


def _begin_run(factory: RecorderFactory, run_id: str, leader: str) -> None:
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


def _enqueue(factory: RecorderFactory, run_id: str, name: str, sequence: int) -> str:
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
    return item.work_item_id


def _seed_scheduler_image(db: LandscapeDB) -> tuple[RecorderFactory, dict[str, str]]:
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
    ids["foreign-ready"] = _enqueue(factory, OTHER_RUN_ID, "foreign-ready", 100)

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
        for name, owner in (("leased-peer-a", PEER_A), ("leased-peer-b", PEER_B)):
            set_item(
                name,
                status=TokenWorkStatus.LEASED.value,
                lease_owner=owner,
                lease_expires_at=NOW + timedelta(seconds=10),
            )
        set_item(
            "leased-peer-equality",
            status=TokenWorkStatus.LEASED.value,
            lease_owner="worker:rm-postgresql-run:equality",
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

    return factory, ids


def test_postgresql_rm01_through_rm06_and_rm09_through_rm13(postgres_db: LandscapeDB) -> None:
    """One durable image proves every scheduler selector and its exclusions."""
    factory, ids = _seed_scheduler_image(postgres_db)
    repository = factory.scheduler

    # RM-01..RM-04: exact status/subtype partitions and peer ownership.
    assert repository.count_unresolved_work(run_id=RUN_ID) == 8
    assert repository.count_unquiesced_work(run_id=RUN_ID) == 5
    assert repository.count_active_work(run_id=RUN_ID) == 11
    assert repository.has_peer_owned_work(run_id=RUN_ID, caller_owner=LEADER) is True

    # RM-05: ready/failed are run scoped, and only a selected continuation's
    # peer owner can authorize relinquishment.
    selected = (ids["ready"], ids["failed"], ids["leased-peer-a"], ids["foreign-ready"])
    assert repository.count_ready_in_set(run_id=RUN_ID, work_item_ids=selected) == 1
    assert repository.count_failed_in_set(run_id=RUN_ID, work_item_ids=selected) == 1
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

    # RM-06: duplicate owners collapse and exact expiry equality is inactive.
    assert repository.peer_active_leases(run_id=RUN_ID, caller_owner=LEADER, now=NOW) == (PEER_A, PEER_B)

    # RM-09..RM-13: active identities, barrier subtype partition, and order.
    assert repository.active_row_ids(run_id=RUN_ID) == frozenset(
        {
            f"row-{name}"
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
    )
    assert repository.blocked_barrier_token_ids(run_id=RUN_ID) == frozenset(
        {"token-blocked-barrier-pending-z", "token-blocked-barrier-adopted-a"}
    )
    assert repository.count_blocked_barrier_items(run_id=RUN_ID) == 2
    assert tuple(item.barrier_key for item in repository.list_blocked_barrier_items(run_id=RUN_ID)) == (
        "a-barrier",
        "z-barrier",
    )
    assert tuple(item.barrier_key for item in repository.list_pending_blocked_barrier_items(run_id=RUN_ID)) == ("z-barrier",)

    # Foreign-run rows never leak into any RM selector.
    assert repository.count_active_work(run_id=OTHER_RUN_ID) == 1
    assert repository.blocked_barrier_token_ids(run_id=OTHER_RUN_ID) == frozenset()


def test_postgresql_rm07_and_rm08_coordination_boundaries(postgres_db: LandscapeDB) -> None:
    factory = make_factory(postgres_db)
    run_id = "rm-postgresql-coordination"
    leader = f"worker:{run_id}:leader"
    _begin_run(factory, run_id, leader)
    coordination = RunCoordinationRepository(postgres_db.engine)

    occupied = coordination.live_leader(run_id=run_id, now=NOW)
    assert occupied is not None
    assert occupied.leader_worker_id == leader
    assert occupied.seat_live is True
    equality = coordination.live_leader(run_id=run_id, now=occupied.leader_heartbeat_expires_at)
    assert equality is not None
    assert equality.seat_live is True
    assert coordination.live_leader(run_id="rm-postgresql-missing", now=NOW) is None

    threshold = NOW - timedelta(seconds=10)
    registered_at = NOW - timedelta(minutes=2)
    workers = (
        ("dead-z", "follower", "active", threshold - timedelta(seconds=1), registered_at),
        ("dead-a", "follower", "active", threshold - timedelta(seconds=1), registered_at),
        ("equality", "follower", "active", threshold, registered_at + timedelta(seconds=1)),
        ("departed", "follower", "departed", threshold - timedelta(seconds=2), registered_at),
    )
    with postgres_db.engine.begin() as conn:
        for worker_id, role, status, expires_at, registered in workers:
            conn.execute(
                insert(run_workers_table).values(
                    worker_id=worker_id,
                    run_id=run_id,
                    role=role,
                    status=status,
                    registered_at=registered,
                    heartbeat_expires_at=expires_at,
                    departed_at=NOW if status == "departed" else None,
                )
            )
    assert coordination.dead_non_leader_workers(
        run_id=run_id,
        leader_worker_id=leader,
        now=NOW,
        grace_seconds=10,
    ) == ("dead-a", "dead-z")

    with postgres_db.engine.begin() as conn:
        conn.execute(
            update(run_coordination_table)
            .where(run_coordination_table.c.run_id == run_id)
            .values(leader_worker_id=None, leader_heartbeat_expires_at=None, updated_at=NOW)
        )
    assert coordination.live_leader(run_id=run_id, now=NOW) is None


def test_postgresql_rm14_accounting_census_and_abandoned_resume_refusal(
    postgres_db: LandscapeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = make_factory(postgres_db)
    run_ids = (
        "rm-postgresql-decided",
        "rm-postgresql-pending",
        "rm-postgresql-abandoned",
        "rm-postgresql-contradiction",
    )
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

    for run_id in (run_ids[0], run_ids[3]):
        factory.data_flow.record_token_outcome(
            ref=TokenRef(token_id=f"token-{run_id}", run_id=run_id),
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name="sink-a",
        )
    with postgres_db.engine.begin() as conn:
        for run_id in (run_ids[2], run_ids[3]):
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

    batch = load_run_accounting_map_from_db(postgres_db, run_ids)
    assert batch.accounting[run_ids[0]].integrity.closure == "closed"
    assert batch.accounting[run_ids[1]].integrity.closure == "open"
    assert batch.accounting[run_ids[1]].tokens.pending == 1
    assert batch.accounting[run_ids[2]].integrity.closure == "abandoned"
    assert batch.accounting[run_ids[2]].tokens.abandoned == 1
    assert run_ids[3] not in batch.accounting
    assert "both terminally decided and marked ABANDONED" in batch.corrupt[run_ids[3]].violations[0]

    recovery = RecoveryManager(postgres_db, checkpoint_manager=object())  # type: ignore[arg-type]
    for checkpoint in (None, object()):
        monkeypatch.setattr(
            recovery,
            "_get_latest_checkpoint_for_resume_workset",
            lambda _run_id, checkpoint=checkpoint: checkpoint,
        )
        for run_id in (run_ids[2], run_ids[3]):
            with pytest.raises(AuditIntegrityError, match="ABANDONED"):
                recovery.get_resume_workset(run_id)


def test_postgresql_token_work_status_check_rejects_unknown_state(postgres_db: LandscapeDB) -> None:
    """The storage vocabulary rejects F-12's removed WAITING state."""
    factory = make_factory(postgres_db)
    run_id = "rm-postgresql-status-check"
    _begin_run(factory, run_id, f"worker:{run_id}:leader")
    work_item_id = _enqueue(factory, run_id, "postgresql-invalid-status", 0)

    with pytest.raises(IntegrityError), postgres_db.engine.begin() as conn:
        conn.execute(update(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id).values(status="waiting"))
