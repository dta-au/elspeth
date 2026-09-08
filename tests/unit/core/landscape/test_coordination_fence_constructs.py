"""Dedicated unit tests for the two SHARED fence constructs (ADR-030 §C.4 / §G).

Design :411: each construct has ONE definition and ONE dedicated unit test —
the ``blocked_barrier_hold_clause`` hygiene pattern — because the predicates
appear in many verbs and per-verb hand-rolled copies would drift.

1. ``active_worker_fence_clause`` and ``claim_verb_fence_clause`` pin the
   SQL predicates independently, including the latter's internal N=0 arm.
   Public scheduler writes require explicit registered membership before
   reaching these predicates: no caller can select an unfenced public path.

2. ``verify_and_extend_leader_fence`` (coordination repository): the leader
   epoch verify-and-extend UPDATE CAS, emitted as the FIRST statement of
   every leader-fenced transaction (design :244-255). It executes on the
   CALLER's connection inside ``begin_write`` — no autonomous commit — so a
   later rollback unwinds the extension with the payload.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from inspect import Parameter, signature

import pytest
from sqlalchemy import CheckConstraint, delete, insert, select, update

from elspeth.contracts import NodeType, PipelineRow, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.errors import AuditIntegrityError, RunLeadershipLostError, RunMembershipLostError, RunWorkerEvictedError
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.landscape.database import LandscapeDB, begin_write
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.run_coordination_repository import (
    RunCoordinationRepository,
    verify_and_extend_leader_fence,
)
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    active_worker_fence_clause,
    claim_verb_fence_clause,
    metadata,
    nodes_table,
    rows_table,
    run_coordination_events_table,
    run_coordination_table,
    run_workers_table,
    runs_table,
    scheduler_events_table,
    token_work_items_table,
    tokens_table,
)
from tests.fixtures.landscape import (
    assert_deadline_within,
    assert_stamped_between,
    expire_lease,
    landscape_database_now,
    make_landscape_db,
    reschedule_work_item,
)
from tests.helpers.run_coordination import register_run_leader

# Forensic seed instant (rows, tokens, registrations). Never a lease decision
# input: deadlines the tests need are written relative to the database clock.
NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)
RUN_1 = "run-fence-construct-1"
RUN_2 = "run-fence-construct-2"
NODE_ID = "transform-1"
SOURCE_NODE_ID = "source-1"


@pytest.fixture
def db() -> LandscapeDB:
    return make_landscape_db()


def _insert_run(db: LandscapeDB, run_id: str) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            insert(runs_table).values(
                run_id=run_id,
                started_at=NOW,
                config_hash="cfg",
                settings_json="{}",
                canonical_version="v1",
                status=RunStatus.RUNNING.value,
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        )
        for node_id, node_type in ((SOURCE_NODE_ID, NodeType.SOURCE), (NODE_ID, NodeType.TRANSFORM)):
            conn.execute(
                insert(nodes_table).values(
                    run_id=run_id,
                    node_id=node_id,
                    plugin_name="test",
                    node_type=node_type.value,
                    plugin_version="1.0",
                    determinism="deterministic",
                    config_hash="cfg",
                    config_json="{}",
                    registered_at=NOW,
                )
            )


def _insert_worker(db: LandscapeDB, *, worker_id: str, run_id: str, status: str) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            insert(run_workers_table).values(
                worker_id=worker_id,
                run_id=run_id,
                role="follower",
                status=status,
                registered_at=NOW,
                heartbeat_expires_at=read_landscape_transaction_time(conn) + timedelta(hours=1),
                evicted_at=NOW if status == "evicted" else None,
            )
        )


def _seed_ready_item(db: LandscapeDB, run_id: str, *, sequence: int = 0) -> str:
    """One READY token_work_items storage seed. Returns work_item_id."""
    token_id = f"token-{run_id}-{sequence}"
    row_id = f"row-{run_id}-{sequence}"
    with db.engine.begin() as conn:
        conn.execute(
            insert(rows_table).values(
                row_id=row_id,
                run_id=run_id,
                source_node_id=SOURCE_NODE_ID,
                row_index=sequence,
                source_row_index=sequence,
                ingest_sequence=sequence,
                source_data_hash=f"hash-{row_id}",
                created_at=NOW,
            )
        )
        conn.execute(insert(tokens_table).values(token_id=token_id, row_id=row_id, run_id=run_id, created_at=NOW))
    # Storage-level seed preserves the empty-membership predicate cases.
    # Public enqueue always requires an active registered member.
    with db.engine.begin() as conn:
        conn.execute(
            insert(token_work_items_table).values(
                work_item_id=f"work-{token_id}",
                run_id=run_id,
                token_id=token_id,
                row_id=row_id,
                node_id=NODE_ID,
                step_index=1,
                ingest_sequence=sequence,
                row_payload_json=TokenSchedulerRepository.serialize_row_payload(
                    PipelineRow({"id": sequence}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
                ),
                status="ready",
                attempt=1,
                available_at=NOW,
                created_at=NOW,
                updated_at=NOW,
                lineage_path_json="[]",
            )
        )
    with db.engine.connect() as conn:
        return str(
            conn.execute(select(token_work_items_table.c.work_item_id).where(token_work_items_table.c.token_id == token_id)).scalar_one()
        )


def _seed_pending_sink_item(db: LandscapeDB, run_id: str, *, sequence: int = 0) -> str:
    from elspeth.contracts.scheduler import TokenWorkStatus

    work_item_id = _seed_ready_item(db, run_id, sequence=sequence)
    with db.engine.begin() as conn:
        conn.execute(
            update(token_work_items_table)
            .where(token_work_items_table.c.work_item_id == work_item_id)
            .values(
                status=TokenWorkStatus.PENDING_SINK.value,
                pending_sink_name="sink-a",
                pending_outcome=TerminalOutcome.SUCCESS.value,
                pending_path=TerminalPath.DEFAULT_FLOW.value,
                pending_error_hash=None,
                pending_error_message=None,
                updated_at=NOW + timedelta(seconds=1),
            )
        )
    return work_item_id


def _seed_unscheduled_item(db: LandscapeDB, run_id: str, *, sequence: int) -> dict[str, object]:
    """Insert the durable row/token prerequisites for one not-yet-queued item."""
    token_id = f"token-{run_id}-{sequence}"
    row_id = f"row-{run_id}-{sequence}"
    with db.engine.begin() as conn:
        conn.execute(
            insert(rows_table).values(
                row_id=row_id,
                run_id=run_id,
                source_node_id=SOURCE_NODE_ID,
                row_index=sequence,
                source_row_index=sequence,
                ingest_sequence=sequence,
                source_data_hash=f"hash-{row_id}",
                created_at=NOW,
            )
        )
        conn.execute(insert(tokens_table).values(token_id=token_id, row_id=row_id, run_id=run_id, created_at=NOW))
    return {
        "token_id": token_id,
        "row_id": row_id,
        "node_id": NODE_ID,
        "step_index": 1,
        "ingest_sequence": sequence,
        "row_payload_json": TokenSchedulerRepository.serialize_row_payload(
            PipelineRow({"id": sequence}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
        ),
        "lease_seconds": 60,
    }


def _full_durable_snapshot(db: LandscapeDB) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Backend-portable value snapshot of every durable Landscape table."""
    with db.engine.connect() as conn:
        return {
            table.name: tuple(sorted((tuple(row) for row in conn.execute(select(table)).all()), key=repr))
            for table in sorted(metadata.tables.values(), key=lambda table: table.name)
        }


def _assert_only_member_refusal(db: LandscapeDB, before: dict[str, tuple[tuple[object, ...], ...]], *, verb: str) -> None:
    after = _full_durable_snapshot(db)
    for table, rows in before.items():
        if table != "run_coordination_events":
            assert after[table] == rows, table
    added = [row for row in after["run_coordination_events"] if row not in before["run_coordination_events"]]
    assert len(added) == 1
    assert len(after["run_coordination_events"]) == len(before["run_coordination_events"]) + 1
    record = dict(zip(run_coordination_events_table.columns.keys(), added[0], strict=True))
    assert record["event_type"] == "fence_refusal"
    assert json.loads(str(record["context_json"])) == {"verb": verb, "fence": "membership"}


class TestActiveWorkerFenceClause:
    """Design :257-265, :411 — the CONSTRUCT only (compilation is slice 4)."""

    def test_compiled_shape_is_correlated_exists_over_run_workers(self) -> None:
        clause = active_worker_fence_clause(worker_id="worker-x", run_id="run-x")
        sql = str(clause.compile(compile_kwargs={"literal_binds": True}))
        assert "EXISTS" in sql.upper()
        assert "run_workers" in sql
        assert "worker_id" in sql
        assert "run_id" in sql
        assert "status = 'active'" in sql

    def test_active_literal_matches_the_run_workers_status_check(self) -> None:
        """The ``blocked_barrier_hold_clause`` literal-parity discipline: the
        'active' literal the clause compiles MUST be a member of the
        ``ck_run_workers_status`` CHECK's value set — drift between the two
        would make the fence silently always-False."""
        status_checks = [
            constraint
            for constraint in run_workers_table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name == "ck_run_workers_status"
        ]
        assert len(status_checks) == 1
        check_sql = str(status_checks[0].sqltext)
        for literal in ("'active'", "'departed'", "'evicted'"):
            assert literal in check_sql
        clause_sql = str(active_worker_fence_clause(worker_id="w", run_id="r").compile(compile_kwargs={"literal_binds": True}))
        assert "'active'" in clause_sql


class TestClaimVerbFenceClause:
    """Pin the internal claim predicate and strict public authority admission."""

    @pytest.mark.parametrize(
        ("registered_worker", "caller", "expected_rowcount"),
        [
            (None, "worker-absent", 1),  # N=0 unit-test mode remains allowed
            ("worker-active", "worker-active", 1),  # active registered caller
            ("worker-active", "worker-absent", 0),  # absent caller cannot bypass active registry
            ("worker-evicted", "worker-absent", 0),  # absent caller cannot bypass any non-empty registry
            ("worker-evicted", "worker-evicted", 0),  # non-active caller remains fenced
        ],
    )
    def test_claim_verb_allows_absent_worker_only_when_run_has_no_workers(
        self,
        db: LandscapeDB,
        registered_worker: str | None,
        caller: str,
        expected_rowcount: int,
    ) -> None:
        _insert_run(db, RUN_1)
        if registered_worker is not None:
            status = "evicted" if registered_worker == "worker-evicted" else "active"
            _insert_worker(db, worker_id=registered_worker, run_id=RUN_1, status=status)
        work_item_id = _seed_ready_item(db, RUN_1)

        with begin_write(db.engine) as conn:
            result = conn.execute(
                update(token_work_items_table)
                .where(
                    token_work_items_table.c.work_item_id == work_item_id,
                    token_work_items_table.c.status == "ready",
                    claim_verb_fence_clause(worker_id=caller, run_id=RUN_1),
                )
                .values(updated_at=NOW + timedelta(seconds=1))
            )

        assert result.rowcount == expected_rowcount

    def test_claim_ready_absent_worker_does_not_claim_when_run_has_active_workers(self, db: LandscapeDB) -> None:
        from elspeth.contracts.scheduler import TokenWorkStatus

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-active", run_id=RUN_1, status="active")
        work_item_id = _seed_ready_item(db, RUN_1)
        repo = TokenSchedulerRepository(db.engine)

        with pytest.raises(AuditIntegrityError, match="unregistered"):
            repo.claim_ready(
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-absent"), lease_owner="worker-absent", lease_seconds=60
            )
        with db.engine.connect() as conn:
            row = conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id)).mappings().one()
        assert row["status"] == TokenWorkStatus.READY.value
        assert row["lease_owner"] is None

    def test_claim_pending_sink_absent_worker_does_not_claim_when_run_has_active_workers(self, db: LandscapeDB) -> None:
        from elspeth.contracts.scheduler import TokenWorkStatus

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-active", run_id=RUN_1, status="active")
        work_item_id = _seed_pending_sink_item(db, RUN_1)
        repo = TokenSchedulerRepository(db.engine)

        with pytest.raises(RunLeadershipLostError):
            repo.claim_pending_sink(
                coordination_token=CoordinationToken(run_id=RUN_1, worker_id="worker-absent", leader_epoch=1),
                lease_owner="worker-absent",
                lease_seconds=60,
            )
        with db.engine.connect() as conn:
            row = conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id)).mappings().one()
        assert row["status"] == TokenWorkStatus.PENDING_SINK.value
        assert row["lease_owner"] is None

    @pytest.mark.parametrize(
        ("caller", "caller_run", "expected_rowcount"),
        [
            ("worker-active", RUN_1, 1),  # A: active, this run
            ("worker-evicted", RUN_1, 0),  # B: evicted, this run
            ("worker-departed", RUN_1, 0),  # C: departed, this run
            ("worker-absent", RUN_1, 0),  # no registry row at all
            ("worker-other-run", RUN_1, 0),  # D: active, but in the OTHER run
        ],
    )
    def test_claim_shaped_update_against_real_epoch_21_db(
        self, db: LandscapeDB, caller: str, caller_run: str, expected_rowcount: int
    ) -> None:
        """Behavioral matrix on a real epoch-21 SQLite DB: the clause embedded
        in a representative claim-shaped UPDATE over one seeded READY row."""
        _insert_run(db, RUN_1)
        _insert_run(db, RUN_2)
        _insert_worker(db, worker_id="worker-active", run_id=RUN_1, status="active")
        _insert_worker(db, worker_id="worker-evicted", run_id=RUN_1, status="evicted")
        _insert_worker(db, worker_id="worker-departed", run_id=RUN_1, status="departed")
        _insert_worker(db, worker_id="worker-other-run", run_id=RUN_2, status="active")
        work_item_id = _seed_ready_item(db, RUN_1)

        with begin_write(db.engine) as conn:
            result = conn.execute(
                update(token_work_items_table)
                .where(
                    token_work_items_table.c.work_item_id == work_item_id,
                    token_work_items_table.c.status == "ready",
                    active_worker_fence_clause(worker_id=caller, run_id=caller_run),
                )
                .values(updated_at=NOW + timedelta(seconds=1))
            )
            assert result.rowcount == expected_rowcount

    def test_membership_fence_compiled_into_claim_verbs_in_slice_4(self, db: LandscapeDB) -> None:
        """Slice-4 flip (design :491): the clause IS compiled into
        ``claim_ready``/``claim_pending_sink``/``enqueue_ready`` — an EVICTED
        worker is refused with ``RunMembershipLostError`` for both member verbs.

        Replaces the slice-2 negative pin (EVICTED worker could claim).
        """
        from elspeth.contracts.scheduler import TokenWorkStatus

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-evicted", run_id=RUN_1, status="evicted")
        _seed_ready_item(db, RUN_1)
        repo = TokenSchedulerRepository(db.engine)

        # claim_ready: evicted worker is refused
        with pytest.raises(RunMembershipLostError) as exc_info:
            repo.claim_ready(
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-evicted"), lease_owner="worker-evicted", lease_seconds=60
            )
        assert exc_info.value.worker_id == "worker-evicted"
        assert exc_info.value.run_id == RUN_1

        # The READY row is untouched (zero mutation on fence failure).
        with db.engine.connect() as conn:
            status = conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == RUN_1)).scalar_one()
        assert status == TokenWorkStatus.READY.value

        # enqueue_ready: the evicted member token is refused.
        # Re-use the same schema seed (different sequence to avoid work_item_id collision).
        _seed_ready_item(db, RUN_1, sequence=1)
        with pytest.raises(RunMembershipLostError):
            repo.enqueue_ready(
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-evicted"),
                token_id="token-new",
                row_id="row-new",
                node_id=NODE_ID,
                step_index=1,
                ingest_sequence=99,
                row_payload_json=TokenSchedulerRepository.serialize_row_payload(
                    PipelineRow({"id": 99}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
                ),
            )

        # An absent registration is an integrity error, before queue mutation.
        with pytest.raises(AuditIntegrityError, match="unregistered") as exc_info2:
            repo.enqueue_ready(
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-absent"),
                token_id="token-absent",
                row_id="row-absent",
                node_id=NODE_ID,
                step_index=1,
                ingest_sequence=100,
                row_payload_json=TokenSchedulerRepository.serialize_row_payload(
                    PipelineRow({"id": 100}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
                ),
            )
        assert "worker-absent" in str(exc_info2.value)

        # An ACTIVE worker can still claim (positive control).
        _insert_worker(db, worker_id="worker-active", run_id=RUN_1, status="active")
        claimed = repo.claim_ready(
            member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-active"), lease_owner="worker-active", lease_seconds=60
        )
        assert claimed is not None, "active worker must succeed"


class TestEnqueueReadyClaimedMembershipFence:
    """Standalone enqueue-and-claim requires current registered membership."""

    @pytest.mark.parametrize("caller_status", [None, "evicted"], ids=["absent", "evicted"])
    def test_absent_or_evicted_identity_is_refused_with_full_zero_mutation(
        self,
        db: LandscapeDB,
        caller_status: str | None,
    ) -> None:
        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-active", run_id=RUN_1, status="active")
        caller = "worker-absent" if caller_status is None else "worker-evicted"
        if caller_status is not None:
            _insert_worker(db, worker_id=caller, run_id=RUN_1, status=caller_status)
        enqueue = _seed_unscheduled_item(db, RUN_1, sequence=10)
        before = _full_durable_snapshot(db)

        expected_error = AuditIntegrityError if caller_status is None else RunMembershipLostError
        with pytest.raises(expected_error) as exc_info:
            TokenSchedulerRepository(db.engine).enqueue_ready_claimed(
                **enqueue, member_token=WorkerMembershipToken(run_id=RUN_1, worker_id=caller), lease_owner=caller
            )
        assert caller in str(exc_info.value)
        if caller_status is None:
            assert _full_durable_snapshot(db) == before
        else:
            _assert_only_member_refusal(db, before, verb="enqueue_ready_claimed")

    def test_active_member_enqueues_claims_and_records_both_events(self, db: LandscapeDB) -> None:
        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-active", run_id=RUN_1, status="active")
        enqueue = _seed_unscheduled_item(db, RUN_1, sequence=11)

        claimed = TokenSchedulerRepository(db.engine).enqueue_ready_claimed(
            **enqueue, member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-active"), lease_owner="worker-active"
        )

        assert claimed.status is TokenWorkStatus.LEASED
        assert claimed.lease_owner == "worker-active"
        with db.engine.connect() as conn:
            event_types = set(
                conn.execute(select(scheduler_events_table.c.event_type).where(scheduler_events_table.c.run_id == RUN_1)).scalars()
            )
        assert event_types == {SchedulerEventType.ENQUEUE.value, SchedulerEventType.CLAIM_READY.value}

    def test_active_member_future_item_stays_ready_without_misreported_eviction(self, db: LandscapeDB) -> None:
        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-active", run_id=RUN_1, status="active")
        enqueue = _seed_unscheduled_item(db, RUN_1, sequence=15)
        repo = TokenSchedulerRepository(db.engine)
        # Enqueue stamps available_at from database time; park the READY row
        # one minute into the DATABASE's future, then replay the idempotent
        # enqueue-and-claim: the claim CAS admits a row only once
        # available_at <= database time (ADR-047).
        ready = repo.enqueue_ready(
            member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-active"),
            **{key: value for key, value in enqueue.items() if key != "lease_seconds"},
        )
        future = reschedule_work_item(db.engine, ready.work_item_id, seconds_from_now=60)

        scheduled = repo.enqueue_ready_claimed(
            **enqueue, member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-active"), lease_owner="worker-active"
        )

        assert scheduled.status is TokenWorkStatus.READY
        assert scheduled.lease_owner is None
        assert scheduled.available_at == future
        with db.engine.connect() as conn:
            event_types = set(
                conn.execute(select(scheduler_events_table.c.event_type).where(scheduler_events_table.c.run_id == RUN_1)).scalars()
            )
        assert event_types == {SchedulerEventType.ENQUEUE.value}

    def test_unregistered_enqueue_and_claim_is_refused_at_n0(self, db: LandscapeDB) -> None:
        _insert_run(db, RUN_1)
        enqueue = _seed_unscheduled_item(db, RUN_1, sequence=12)
        before = _full_durable_snapshot(db)
        with pytest.raises(AuditIntegrityError, match="unregistered"):
            TokenSchedulerRepository(db.engine).enqueue_ready_claimed(
                **enqueue,
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="unregistered"),
                lease_owner="unregistered",
            )
        assert _full_durable_snapshot(db) == before

    def test_membership_cas_rolls_back_when_member_is_evicted_after_entry_guard(
        self,
        db: LandscapeDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The claim UPDATE rechecks membership after the initial strict guard."""
        from elspeth.core.landscape.scheduler import queue as queue_module

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-active", run_id=RUN_1, status="active")
        enqueue = _seed_unscheduled_item(db, RUN_1, sequence=13)
        before = _full_durable_snapshot(db)
        real_insert = queue_module.insert_work_item_idempotent

        def evict_then_insert(*args: object, **kwargs: object) -> bool:
            conn = args[0]
            conn.execute(
                update(run_workers_table).where(run_workers_table.c.worker_id == "worker-active").values(status="evicted", evicted_at=NOW)
            )
            return real_insert(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(queue_module, "insert_work_item_idempotent", evict_then_insert)

        with pytest.raises(RunWorkerEvictedError):
            TokenSchedulerRepository(db.engine).enqueue_ready_claimed(
                **enqueue, member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-active"), lease_owner="worker-active"
            )

        assert _full_durable_snapshot(db) == before

    def test_strict_membership_cas_rolls_back_when_sole_member_is_deleted_after_entry_guard(
        self,
        db: LandscapeDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Production enqueue must not fall through to the lenient N=0 claim arm."""
        from elspeth.core.landscape.scheduler import queue as queue_module

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-active", run_id=RUN_1, status="active")
        enqueue = _seed_unscheduled_item(db, RUN_1, sequence=14)
        before = _full_durable_snapshot(db)
        real_insert = queue_module.insert_work_item_idempotent

        def delete_member_then_insert(*args: object, **kwargs: object) -> bool:
            conn = args[0]
            conn.execute(delete(run_workers_table).where(run_workers_table.c.worker_id == "worker-active"))
            return real_insert(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(queue_module, "insert_work_item_idempotent", delete_member_then_insert)

        with pytest.raises(RunWorkerEvictedError) as exc_info:
            TokenSchedulerRepository(db.engine).enqueue_ready_claimed(
                **enqueue, member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-active"), lease_owner="worker-active"
            )

        assert exc_info.value.worker_id == "worker-active"
        assert exc_info.value.run_id == RUN_1
        assert _full_durable_snapshot(db) == before


class TestHeartbeatLeaseMembershipFence:
    """Heartbeat callers must carry an explicit registered member token."""

    def test_public_repository_layers_require_explicit_membership(self) -> None:
        from elspeth.core.landscape.scheduler.leases import SchedulerLeaseRepository

        for heartbeat in (SchedulerLeaseRepository.heartbeat_lease, TokenSchedulerRepository.heartbeat_lease):
            parameter = signature(heartbeat).parameters["member_token"]
            assert parameter.default is Parameter.empty

    @staticmethod
    def _leased_item(db: LandscapeDB, *, worker_id: str) -> tuple[TokenSchedulerRepository, str]:
        work_item_id = _seed_ready_item(db, RUN_1)
        repo = TokenSchedulerRepository(db.engine)
        claimed = repo.claim_ready(
            member_token=WorkerMembershipToken(run_id=RUN_1, worker_id=worker_id), lease_owner=worker_id, lease_seconds=60
        )
        assert claimed is not None and claimed.work_item_id == work_item_id
        return repo, work_item_id

    @pytest.mark.parametrize("status", ["evicted", "departed"])
    def test_non_active_owner_is_refused_with_zero_durable_mutation(self, db: LandscapeDB, status: str) -> None:

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-a", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="worker-a")
        with db.engine.begin() as conn:
            conn.execute(
                update(run_workers_table)
                .where(run_workers_table.c.worker_id == "worker-a")
                .values(
                    status=status,
                    departed_at=NOW + timedelta(seconds=1) if status == "departed" else None,
                    evicted_at=NOW + timedelta(seconds=1) if status == "evicted" else None,
                    evicted_by_worker_id="worker-leader" if status == "evicted" else None,
                )
            )
        before = _full_durable_snapshot(db)

        with pytest.raises(RunMembershipLostError) as exc_info:
            repo.heartbeat_lease(
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-a"),
                work_item_id=work_item_id,
                lease_owner="worker-a",
                lease_seconds=60,
            )

        assert exc_info.value.worker_id == "worker-a"
        assert exc_info.value.run_id == RUN_1
        _assert_only_member_refusal(db, before, verb="heartbeat_lease")

    def test_deleted_sole_membership_row_is_refused_with_zero_durable_mutation(self, db: LandscapeDB) -> None:

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-a", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="worker-a")
        with db.engine.begin() as conn:
            conn.execute(delete(run_workers_table).where(run_workers_table.c.worker_id == "worker-a"))
        before = _full_durable_snapshot(db)

        with pytest.raises(AuditIntegrityError, match="unregistered"):
            repo.heartbeat_lease(
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-a"),
                work_item_id=work_item_id,
                lease_owner="worker-a",
                lease_seconds=60,
            )

        assert _full_durable_snapshot(db) == before

    def test_active_current_owner_can_extend_lease(self, db: LandscapeDB) -> None:
        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-a", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="worker-a")

        before = landscape_database_now(db.engine)
        expires_at = repo.heartbeat_lease(
            member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-a"),
            work_item_id=work_item_id,
            lease_owner="worker-a",
            lease_seconds=60,
        )
        after = landscape_database_now(db.engine)

        assert_stamped_between(expires_at, start=before, end=after, offset=timedelta(seconds=60))

    def test_active_owner_can_revive_expired_lease_before_recovery(self, db: LandscapeDB) -> None:
        """Expiry alone is not ownership loss; recovery is the competing CAS."""
        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-a", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="worker-a")
        expired_at = expire_lease(db.engine, work_item_id)

        before = landscape_database_now(db.engine)
        expires_at = repo.heartbeat_lease(
            member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-a"),
            work_item_id=work_item_id,
            lease_owner="worker-a",
            lease_seconds=60,
        )
        after = landscape_database_now(db.engine)

        assert expires_at > expired_at
        assert_stamped_between(expires_at, start=before, end=after, offset=timedelta(seconds=60))

    def test_wrong_active_owner_uses_existing_lease_lost_path(self, db: LandscapeDB) -> None:
        from elspeth.contracts.errors import SchedulerLeaseLostError

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-a", run_id=RUN_1, status="active")
        _insert_worker(db, worker_id="worker-b", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="worker-a")

        with pytest.raises(SchedulerLeaseLostError):
            repo.heartbeat_lease(
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-b"),
                work_item_id=work_item_id,
                lease_owner="worker-b",
                lease_seconds=60,
            )

    def test_recovered_lease_uses_existing_lease_lost_path_when_membership_active(self, db: LandscapeDB) -> None:
        from elspeth.contracts.errors import SchedulerLeaseLostError

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-a", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="worker-a")
        expire_lease(db.engine, work_item_id, seconds_ago=5)
        reaper = register_run_leader(RunCoordinationRepository(db.engine), run_id=RUN_1, worker_id="worker-reaper", window_seconds=80)
        recovered = repo.recover_expired_leases(coordination_token=reaper, stall_budget_seconds=1)
        assert recovered == 1

        with pytest.raises(SchedulerLeaseLostError):
            repo.heartbeat_lease(
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-a"),
                work_item_id=work_item_id,
                lease_owner="worker-a",
                lease_seconds=60,
            )

    def test_missing_membership_after_claim_cannot_extend_lease(self, db: LandscapeDB) -> None:
        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="direct-harness", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="direct-harness")
        with db.engine.begin() as conn:
            conn.execute(delete(run_workers_table).where(run_workers_table.c.worker_id == "direct-harness"))
        before = _full_durable_snapshot(db)
        with pytest.raises(AuditIntegrityError, match="unregistered"):
            repo.heartbeat_lease(
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="direct-harness"),
                work_item_id=work_item_id,
                lease_owner="direct-harness",
                lease_seconds=60,
            )
        assert _full_durable_snapshot(db) == before


class TestVerifyAndExtendLeaderFence:
    """Design :244-255 — the leader epoch verify-and-extend UPDATE CAS."""

    def _seat(self, db: LandscapeDB) -> CoordinationToken:
        _insert_run(db, RUN_1)
        return register_run_leader(
            RunCoordinationRepository(db.engine),
            run_id=RUN_1,
            worker_id="worker-leader",
            window_seconds=80.0,
        )

    def _expiry(self, db: LandscapeDB) -> datetime:
        with db.engine.connect() as conn:
            value: datetime = conn.execute(
                select(run_coordination_table.c.leader_heartbeat_expires_at).where(run_coordination_table.c.run_id == RUN_1)
            ).scalar_one()
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def test_match_extends_expiry_and_stamps_updated_at(self, db: LandscapeDB) -> None:
        token = self._seat(db)
        with begin_write(db.engine) as conn:
            database_now = read_landscape_transaction_time(conn)
            verify_and_extend_leader_fence(conn, token=token, window_seconds=120.0, verb="unit-test")
        # Both stamps come from the database clock, never from a caller's
        # ``now`` (ADR-047); SQLite stamps whole seconds.
        assert_deadline_within(self._expiry(db), database_now + timedelta(seconds=120))
        with db.engine.connect() as conn:
            updated_at = conn.execute(
                select(run_coordination_table.c.updated_at).where(run_coordination_table.c.run_id == RUN_1)
            ).scalar_one()
        assert_deadline_within(updated_at, database_now)

    def test_stale_epoch_raises_and_does_not_move_expiry(self, db: LandscapeDB) -> None:
        token = self._seat(db)
        before = self._expiry(db)
        stale = CoordinationToken(run_id=RUN_1, worker_id=token.worker_id, leader_epoch=token.leader_epoch + 1)
        with pytest.raises(RunLeadershipLostError) as exc_info, begin_write(db.engine) as conn:
            verify_and_extend_leader_fence(conn, token=stale, window_seconds=120.0, verb="unit-test")
        assert exc_info.value.verb == "unit-test"
        assert self._expiry(db) == before

    def test_wrong_worker_with_correct_epoch_raises(self, db: LandscapeDB) -> None:
        token = self._seat(db)
        before = self._expiry(db)
        foreign = CoordinationToken(run_id=RUN_1, worker_id="worker-imposter", leader_epoch=token.leader_epoch)
        with pytest.raises(RunLeadershipLostError), begin_write(db.engine) as conn:
            verify_and_extend_leader_fence(conn, token=foreign, window_seconds=120.0, verb="unit-test")
        assert self._expiry(db) == before

    def test_verify_rides_the_payload_transaction_no_autonomous_commit(self, db: LandscapeDB) -> None:
        """Slice-1 discipline: the verify executes on the CALLER's connection
        inside ``begin_write``. A rollback AFTER a successful verify leaves
        the expiry unmoved — the extension is part of the payload
        transaction, never an autonomous commit."""
        token = self._seat(db)
        before = self._expiry(db)

        class _Boom(Exception):
            pass

        with pytest.raises(_Boom), begin_write(db.engine) as conn:
            verify_and_extend_leader_fence(conn, token=token, window_seconds=120.0, verb="unit-test")
            raise _Boom

        assert self._expiry(db) == before, "the successful verify rolled back with the payload"


class TestDispositionMembershipFence:
    """Disposition requires active membership and the current item owner.

    Evicted or departed identities raise RunMembershipLostError before any
    payload write. Missing registration is an AuditIntegrityError, including
    when the deleted row was the run's sole member. Omitted authority never
    selects an unfenced path.
    """

    def _leased_item(self, db: LandscapeDB, *, worker_id: str, sequence: int = 0) -> tuple[TokenSchedulerRepository, str]:
        work_item_id = _seed_ready_item(db, RUN_1, sequence=sequence)
        repo = TokenSchedulerRepository(db.engine)
        claimed = repo.claim_ready(
            member_token=WorkerMembershipToken(run_id=RUN_1, worker_id=worker_id), lease_owner=worker_id, lease_seconds=60
        )
        assert claimed is not None and claimed.work_item_id == work_item_id
        return repo, work_item_id

    @staticmethod
    def _set_worker_status(db: LandscapeDB, worker_id: str, status: str) -> None:
        with db.engine.begin() as conn:
            conn.execute(
                update(run_workers_table)
                .where(run_workers_table.c.worker_id == worker_id)
                .values(status=status, evicted_at=NOW if status == "evicted" else None)
            )

    @staticmethod
    def _item_state(db: LandscapeDB, work_item_id: str) -> tuple[str, str | None]:
        with db.engine.connect() as conn:
            row = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.lease_owner).where(
                    token_work_items_table.c.work_item_id == work_item_id
                )
            ).one()
        return str(row.status), row.lease_owner

    def test_evicted_worker_is_refused_on_every_disposition_verb_with_zero_mutation(self, db: LandscapeDB) -> None:
        from elspeth.contracts.scheduler import TokenWorkStatus

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-a", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="worker-a")
        self._set_worker_status(db, "worker-a", "evicted")

        dispositions = {
            "mark_terminal": lambda: repo.mark_terminal(
                work_item_id=work_item_id,
                expected_lease_owner="worker-a",
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-a"),
            ),
            "mark_failed": lambda: repo.mark_failed(
                work_item_id=work_item_id,
                expected_lease_owner="worker-a",
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-a"),
            ),
            "mark_blocked": lambda: repo.mark_blocked(
                work_item_id=work_item_id,
                queue_key=None,
                barrier_key="barrier-1",
                expected_lease_owner="worker-a",
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-a"),
            ),
            "mark_pending_sink": lambda: repo.mark_pending_sink(
                work_item_id=work_item_id,
                row_payload_json="{}",
                sink_name="sink-a",
                outcome="success",
                path=TerminalPath.DEFAULT_FLOW.value,
                error_hash=None,
                error_message=None,
                expected_lease_owner="worker-a",
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-a"),
            ),
        }
        for verb, disposition in dispositions.items():
            with pytest.raises(RunMembershipLostError) as exc_info:
                disposition()
            assert exc_info.value.worker_id == "worker-a", verb
            assert exc_info.value.run_id == RUN_1, verb
            status, lease_owner = self._item_state(db, work_item_id)
            assert status == TokenWorkStatus.LEASED.value, f"{verb} mutated status after eviction"
            assert lease_owner == "worker-a", f"{verb} mutated lease_owner after eviction"

    def test_departed_worker_is_refused_too(self, db: LandscapeDB) -> None:

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-a", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="worker-a")
        self._set_worker_status(db, "worker-a", "departed")
        with pytest.raises(RunMembershipLostError):
            repo.mark_terminal(
                work_item_id=work_item_id,
                expected_lease_owner="worker-a",
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-a"),
            )

    def test_fenced_disposition_refuses_when_sole_member_was_deleted(self, db: LandscapeDB) -> None:
        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-unregistered", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="worker-unregistered")
        with db.engine.begin() as conn:
            conn.execute(delete(run_workers_table).where(run_workers_table.c.worker_id == "worker-unregistered"))
        before = _full_durable_snapshot(db)
        with pytest.raises(AuditIntegrityError, match="unregistered"):
            repo.mark_terminal(
                work_item_id=work_item_id,
                expected_lease_owner="worker-unregistered",
                member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-unregistered"),
            )
        assert _full_durable_snapshot(db) == before

    def test_active_worker_disposition_succeeds_with_fence(self, db: LandscapeDB) -> None:
        from elspeth.contracts.scheduler import TokenWorkStatus

        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-a", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="worker-a")
        item = repo.mark_terminal(
            work_item_id=work_item_id,
            expected_lease_owner="worker-a",
            member_token=WorkerMembershipToken(run_id=RUN_1, worker_id="worker-a"),
        )
        assert item.status is TokenWorkStatus.TERMINAL

    def test_omitted_membership_does_not_bypass_eviction(self, db: LandscapeDB) -> None:
        _insert_run(db, RUN_1)
        _insert_worker(db, worker_id="worker-a", run_id=RUN_1, status="active")
        repo, work_item_id = self._leased_item(db, worker_id="worker-a")
        self._set_worker_status(db, "worker-a", "evicted")
        before = _full_durable_snapshot(db)
        with pytest.raises(TypeError, match="member_token"):
            repo.mark_terminal(work_item_id=work_item_id, expected_lease_owner="worker-a")
        assert _full_durable_snapshot(db) == before
