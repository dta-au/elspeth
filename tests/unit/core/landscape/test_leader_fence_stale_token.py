"""Deterministic stale-token fence suite (ADR-030 §H, slice-2 step 4).

For every leader-fenced verb: bump ``leader_epoch`` directly (simulating a
takeover that deposed this worker), then call the verb with the now-stale
:class:`CoordinationToken` and assert the designed refusal contract —

1. :class:`RunLeadershipLostError` raised;
2. exactly one ``fence_refusal`` event recorded (fresh connection, naming the
   verb in ``context_json``);
3. ZERO payload mutation (the verify-and-extend fence is the FIRST statement
   of the verb's IMMEDIATE transaction, so rowcount-0 unwinds everything).

Fenced verbs covered: ``complete_run``, ``update_run_status``,
``create_checkpoint``, ``delete_checkpoints``, ``complete_barrier`` (both the
strict F1 arm and the legacy partial-release wrapper arm),
``ingest_row_with_initial_claim`` (woken-mid-ingest: atomic rollback, no
orphan ``rows`` row), the fenced ``create_row_with_token`` arm,
``reset_adoption_marker_to_pending`` (the §E.3 crash-window reset: fenced
because a SECOND takeover deposes a leader still inside ``restore_from_journal``
— elspeth-ee18e446ff), ``recover_expired_leases``,
``terminalize_pending_sinks_with_terminal_outcomes``, and the §C.4 row-7
per-terminalization-batch fences on ``mark_pending_sink_terminal``/``_many``.

Plus the owner-strictness refusals of the strict pending-sink
terminalization (``mark_pending_sink_terminal``/``_many``): the required
keyword, owner mismatch, and the removed NULL-owner acceptance all refuse
with zero mutation — the owner CAS protects even token-less callers; the
epoch fence stacks on top when a token is threaded.

The §C.4 "fence doubles as the seat heartbeat" property and the §D
quiescence predicate are pinned by the valid-token tests at the bottom.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, insert, select, update

from elspeth.contracts import CheckpointDraft, ExportStatus, NodeType, RunStatus
from elspeth.contracts.audit import SecretResolutionInput
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.errors import (
    AuditIntegrityError,
    OrchestrationInvariantError,
    RunLeadershipLostError,
    RunMembershipLostError,
)
from elspeth.contracts.preflight import CommencementGateResult, PreflightResult
from elspeth.contracts.scheduler import BlockedPendingSinkHandoff, TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.checkpoint.manager import CheckpointManager
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import (
    RunCoordinationRepository,
    fenced_member_transaction,
)
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    batch_members_table,
    batches_table,
    checkpoints_table,
    group_losses_table,
    nodes_table,
    preflight_results_table,
    rows_table,
    run_coordination_events_table,
    run_coordination_table,
    run_sources_table,
    run_workers_table,
    runs_table,
    scheduler_events_table,
    secret_resolutions_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
)
from tests.fixtures.landscape import expire_lease, make_landscape_db
from tests.helpers.run_coordination import register_run_leader

RUN_ID = "run-fence-1"
OTHER_RUN_ID = "run-fence-2"
WORKER = f"worker:{RUN_ID}:deadbeef"
NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)
NODE_ID = "transform-1"
SOURCE_NODE_ID = "source-1"


def _payload_json() -> str:
    return TokenSchedulerRepository.serialize_row_payload(PipelineRow({"id": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True)))


def _checkpoint_draft(sequence_number: int) -> CheckpointDraft:
    return CheckpointDraft(
        run_id=RUN_ID,
        sequence_number=sequence_number,
        upstream_topology_hash="a" * 64,
        barrier_scalars=None,
    )


@pytest.fixture
def db() -> LandscapeDB:
    return make_landscape_db()


@pytest.fixture
def token(db: LandscapeDB) -> CoordinationToken:
    """Seed a RUNNING run + nodes and mint the epoch-1 leader seat."""
    with db.engine.begin() as conn:
        conn.execute(
            insert(runs_table).values(
                run_id=RUN_ID,
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
                    run_id=RUN_ID,
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
    return register_run_leader(RunCoordinationRepository(db.engine), run_id=RUN_ID, worker_id=WORKER, window_seconds=80.0)


def _bump_epoch(db: LandscapeDB) -> None:
    """Depose the leader: a takeover bumped the seat epoch out from under it."""
    with db.engine.begin() as conn:
        conn.execute(
            update(run_coordination_table)
            .where(run_coordination_table.c.run_id == RUN_ID)
            .values(leader_epoch=run_coordination_table.c.leader_epoch + 1)
        )


def _run_lifecycle_snapshot(db: LandscapeDB) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Every table a RunLifecycleRepository writer can touch, as a complete image."""
    snapshot: dict[str, tuple[tuple[object, ...], ...]] = {}
    with db.engine.connect() as conn:
        for table in (runs_table, run_sources_table, secret_resolutions_table, preflight_results_table):
            snapshot[table.name] = tuple(tuple(row) for row in conn.execute(select(table).order_by(*table.primary_key.columns)).all())
    return snapshot


def _fence_refusals(db: LandscapeDB, verb: str) -> list[dict[str, object]]:
    with db.engine.connect() as conn:
        rows = (
            conn.execute(
                select(run_coordination_events_table)
                .where(run_coordination_events_table.c.run_id == RUN_ID)
                .where(run_coordination_events_table.c.event_type == "fence_refusal")
                .order_by(run_coordination_events_table.c.seq)
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows if json.loads(str(row["context_json"])).get("verb") == verb]


def _seat_image(db: LandscapeDB) -> tuple[object, ...]:
    """The seat row as a whole tuple — the zero-mutation witness for a refused seat write."""
    with db.engine.connect() as conn:
        return tuple(conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == RUN_ID)).one())


def _worker_image(db: LandscapeDB, worker_id: str) -> tuple[object, ...]:
    """A ``run_workers`` row as a whole tuple — the zero-mutation witness for a refused member write."""
    with db.engine.connect() as conn:
        return tuple(conn.execute(select(run_workers_table).where(run_workers_table.c.worker_id == worker_id)).one())


def _depart_member(db: LandscapeDB, worker_id: str) -> None:
    """Leave ``active`` by the follower's own exit; single-use identity — it never returns."""
    with db.engine.begin() as conn:
        conn.execute(
            update(run_workers_table)
            .where(run_workers_table.c.worker_id == worker_id)
            .values(status="departed", departed_at=read_landscape_transaction_time(conn))
        )


def _evict_member(db: LandscapeDB, worker_id: str) -> None:
    """Leave ``active`` by the leader's §C.2 housekeeping sweep — the other stale-membership cause."""
    with db.engine.begin() as conn:
        conn.execute(
            update(run_workers_table)
            .where(run_workers_table.c.worker_id == worker_id)
            .values(status="evicted", evicted_at=read_landscape_transaction_time(conn), evicted_by_worker_id="worker:sweep")
        )


def _seed_row_and_token(db: LandscapeDB, *, sequence: int) -> tuple[str, str]:
    token_id = f"token-{sequence}"
    row_id = f"row-{sequence}"
    with db.engine.begin() as conn:
        conn.execute(
            insert(rows_table).values(
                row_id=row_id,
                run_id=RUN_ID,
                source_node_id=SOURCE_NODE_ID,
                row_index=sequence,
                source_row_index=sequence,
                ingest_sequence=sequence,
                source_data_hash=f"hash-{row_id}",
                created_at=NOW,
            )
        )
        conn.execute(insert(tokens_table).values(token_id=token_id, row_id=row_id, run_id=RUN_ID, created_at=NOW))
    return token_id, row_id


def _ensure_active_worker(db: LandscapeDB, worker_id: str) -> None:
    with db.engine.begin() as conn:
        existing = conn.execute(
            select(run_workers_table.c.worker_id)
            .where(run_workers_table.c.run_id == RUN_ID)
            .where(run_workers_table.c.worker_id == worker_id)
        ).first()
        if existing is not None:
            return
        conn.execute(
            insert(run_workers_table).values(
                worker_id=worker_id,
                run_id=RUN_ID,
                role="follower",
                status="active",
                registered_at=NOW,
                heartbeat_expires_at=read_landscape_transaction_time(conn) + timedelta(hours=1),
            )
        )


def _work_item_row(db: LandscapeDB, token_id: str) -> dict[str, object]:
    with db.engine.connect() as conn:
        row = conn.execute(select(token_work_items_table).where(token_work_items_table.c.token_id == token_id)).mappings().one()
    return dict(row)


def _barrier_mutation_snapshot(db: LandscapeDB) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Capture every durable surface a refused barrier write could touch."""
    tables = (
        rows_table,
        tokens_table,
        token_work_items_table,
        scheduler_events_table,
        run_coordination_table,
        run_coordination_events_table,
        group_losses_table,
        batches_table,
        batch_members_table,
        token_outcomes_table,
    )
    with db.engine.connect() as conn:
        return {table.name: tuple(tuple(row) for row in conn.execute(select(table))) for table in tables}


def _lease_recovery_mutation_snapshot(db: LandscapeDB) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Capture every durable surface strict lease recovery can mutate."""
    tables = (
        token_work_items_table,
        scheduler_events_table,
        run_coordination_table,
        run_coordination_events_table,
    )
    with db.engine.connect() as conn:
        return {table.name: tuple(tuple(row) for row in conn.execute(select(table))) for table in tables}


def _lease_recovery_run_snapshot(db: LandscapeDB, run_id: str) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Capture recovery-mutable rows for one run."""
    tables = (
        token_work_items_table,
        scheduler_events_table,
        run_coordination_table,
        run_coordination_events_table,
    )
    with db.engine.connect() as conn:
        return {table.name: tuple(tuple(row) for row in conn.execute(select(table).where(table.c.run_id == run_id))) for table in tables}


def _seed_other_run_expired_lease(db: LandscapeDB, repo: TokenSchedulerRepository) -> str:
    token_id = "token-other-run"
    row_id = "row-other-run"
    with db.engine.begin() as conn:
        conn.execute(
            insert(runs_table).values(
                run_id=OTHER_RUN_ID,
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
                    run_id=OTHER_RUN_ID,
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
        conn.execute(
            insert(rows_table).values(
                row_id=row_id,
                run_id=OTHER_RUN_ID,
                source_node_id=SOURCE_NODE_ID,
                row_index=0,
                source_row_index=0,
                ingest_sequence=0,
                source_data_hash="hash-other-run",
                created_at=NOW,
            )
        )
        conn.execute(insert(tokens_table).values(token_id=token_id, row_id=row_id, run_id=OTHER_RUN_ID, created_at=NOW))
    repo.enqueue_ready(
        run_id=OTHER_RUN_ID,
        token_id=token_id,
        row_id=row_id,
        node_id=NODE_ID,
        step_index=1,
        ingest_sequence=0,
        row_payload_json=_payload_json(),
    )
    claimed = repo.claim_ready(run_id=OTHER_RUN_ID, lease_owner="other-run-crashed-worker", lease_seconds=60)
    assert claimed is not None and claimed.token_id == token_id
    expire_lease(db.engine, claimed.work_item_id)
    return token_id


def _enqueue_and_claim(db: LandscapeDB, repo: TokenSchedulerRepository, *, sequence: int, owner: str) -> tuple[str, str, str]:
    """READY → LEASED row for ``owner``; returns (token_id, row_id, work_item_id)."""
    token_id, row_id = _seed_row_and_token(db, sequence=sequence)
    repo.enqueue_ready(
        run_id=RUN_ID,
        token_id=token_id,
        row_id=row_id,
        node_id=NODE_ID,
        step_index=1,
        ingest_sequence=sequence,
        row_payload_json=_payload_json(),
    )
    _ensure_active_worker(db, owner)
    claimed = repo.claim_ready(run_id=RUN_ID, lease_owner=owner, lease_seconds=60)
    assert claimed is not None and claimed.token_id == token_id
    return token_id, row_id, claimed.work_item_id


class TestMissingTokenBarrierRefusals:
    """Strict barrier wrappers reject runtime None before any transaction."""

    def test_complete_barrier_runtime_none_refuses_before_transaction(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner=WORKER)
        repo.mark_blocked(work_item_id=work_item_id, queue_key=None, barrier_key="b1", expected_lease_owner=WORKER)
        before = _barrier_mutation_snapshot(db)
        transactions: list[object] = []

        def record_begin(conn: object) -> None:
            transactions.append(conn)

        event.listen(db.engine, "begin", record_begin)
        try:
            with pytest.raises(TypeError, match="coordination_token"):
                repo.complete_barrier(
                    run_id=RUN_ID,
                    barrier_key="b1",
                    consumed_token_ids=(token_id,),
                    emitted_pending_sink=(),
                    emitted_ready=(),
                    coordination_token=None,  # type: ignore[arg-type]  # runtime trust-boundary regression
                )
        finally:
            event.remove(db.engine, "begin", record_begin)

        assert transactions == [], "missing authority must be refused before opening a transaction"
        assert _barrier_mutation_snapshot(db) == before

    def test_recover_expired_leases_runtime_none_refuses_before_transaction(
        self,
        db: LandscapeDB,
        token: CoordinationToken,
    ) -> None:
        repo = TokenSchedulerRepository(db.engine)
        _token_id, _row_id, _work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner="peer-worker")
        before = _barrier_mutation_snapshot(db)
        transactions: list[object] = []

        def record_begin(conn: object) -> None:
            transactions.append(conn)

        event.listen(db.engine, "begin", record_begin)
        try:
            with pytest.raises(TypeError, match="coordination_token"):
                repo.recover_expired_leases(
                    coordination_token=None,  # type: ignore[arg-type]  # runtime trust-boundary regression
                )
        finally:
            event.remove(db.engine, "begin", record_begin)

        assert transactions == [], "missing authority must be refused before opening a transaction"
        assert _barrier_mutation_snapshot(db) == before

    def test_named_legacy_recovery_adapter_recovers_direct_harness_lease(
        self,
        db: LandscapeDB,
        token: CoordinationToken,
    ) -> None:
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner="direct-harness")
        expire_lease(db.engine, work_item_id)

        recovered = repo.recover_expired_leases_legacy_unfenced(
            run_id=RUN_ID,
            caller_owner=WORKER,
        )

        assert recovered == 1
        assert _work_item_row(db, token_id)["status"] == TokenWorkStatus.READY.value

    def test_terminal_wrapper_runtime_none_refuses_before_transaction(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner=WORKER)
        repo.mark_blocked(work_item_id=work_item_id, queue_key=None, barrier_key="b1", expected_lease_owner=WORKER)
        before = _barrier_mutation_snapshot(db)
        transactions: list[object] = []

        def record_begin(conn: object) -> None:
            transactions.append(conn)

        event.listen(db.engine, "begin", record_begin)
        try:
            with pytest.raises(TypeError, match="coordination_token"):
                repo.mark_blocked_barrier_terminal(
                    run_id=RUN_ID,
                    barrier_key="b1",
                    token_ids=(token_id,),
                    coordination_token=None,  # type: ignore[arg-type]  # runtime trust-boundary regression
                )
        finally:
            event.remove(db.engine, "begin", record_begin)

        assert transactions == [], "missing authority must be refused before opening a transaction"
        assert _barrier_mutation_snapshot(db) == before

    def test_pending_sink_wrapper_runtime_none_refuses_before_transaction(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner=WORKER)
        repo.mark_blocked(work_item_id=work_item_id, queue_key=None, barrier_key="b1", expected_lease_owner=WORKER)
        before = _barrier_mutation_snapshot(db)
        transactions: list[object] = []

        def record_begin(conn: object) -> None:
            transactions.append(conn)

        event.listen(db.engine, "begin", record_begin)
        try:
            with pytest.raises(TypeError, match="coordination_token"):
                repo.mark_blocked_barrier_pending_sink_many(
                    run_id=RUN_ID,
                    barrier_key="b1",
                    handoffs={
                        token_id: BlockedPendingSinkHandoff(
                            row_payload_json=_payload_json(),
                            sink_name="sink-a",
                            outcome="success",
                            path="completed",
                            error_hash=None,
                            error_message=None,
                        )
                    },
                    coordination_token=None,  # type: ignore[arg-type]  # runtime trust-boundary regression
                )
        finally:
            event.remove(db.engine, "begin", record_begin)

        assert transactions == [], "missing authority must be refused before opening a transaction"
        assert _barrier_mutation_snapshot(db) == before


class TestStrictRecoveryScopeBinding:
    """Strict recovery derives run and caller scope from its authority token."""

    def test_supported_call_only_recovers_token_run(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        run_token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner="peer-worker")
        expire_lease(db.engine, work_item_id)
        with db.engine.begin() as conn:
            conn.execute(
                update(run_workers_table)
                .where(run_workers_table.c.run_id == RUN_ID)
                .where(run_workers_table.c.worker_id == "peer-worker")
                .values(status="departed", departed_at=NOW)
            )
        other_token_id = _seed_other_run_expired_lease(db, repo)
        other_run_before = _lease_recovery_run_snapshot(db, OTHER_RUN_ID)

        recovered = repo.recover_expired_leases(
            coordination_token=token,
        )

        assert recovered == 1
        with db.engine.connect() as conn:
            status_rows = conn.execute(
                select(token_work_items_table.c.token_id, token_work_items_table.c.status).where(
                    token_work_items_table.c.token_id.in_((run_token_id, other_token_id))
                )
            ).all()
            statuses = {row.token_id: row.status for row in status_rows}
        assert statuses == {
            run_token_id: TokenWorkStatus.READY.value,
            other_token_id: TokenWorkStatus.LEASED.value,
        }
        assert _lease_recovery_run_snapshot(db, OTHER_RUN_ID) == other_run_before

    def test_cross_run_scope_argument_refuses_before_transaction(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        _seed_other_run_expired_lease(db, repo)
        before = _lease_recovery_mutation_snapshot(db)
        transactions: list[object] = []

        def record_begin(conn: object) -> None:
            transactions.append(conn)

        event.listen(db.engine, "begin", record_begin)
        try:
            with pytest.raises(TypeError, match="run_id"):
                repo.recover_expired_leases(
                    run_id=OTHER_RUN_ID,
                    caller_owner=WORKER,
                    coordination_token=token,
                )
        finally:
            event.remove(db.engine, "begin", record_begin)

        assert transactions == [], "caller-supplied run scope must be refused before BEGIN"
        assert _lease_recovery_mutation_snapshot(db) == before

    def test_caller_owner_argument_refuses_before_transaction(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        _token_id, _row_id, _work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner="peer-worker")
        with db.engine.begin() as conn:
            conn.execute(
                update(run_workers_table)
                .where(run_workers_table.c.run_id == RUN_ID)
                .where(run_workers_table.c.worker_id == "peer-worker")
                .values(status="departed", departed_at=NOW)
            )
        before = _lease_recovery_mutation_snapshot(db)
        transactions: list[object] = []

        def record_begin(conn: object) -> None:
            transactions.append(conn)

        event.listen(db.engine, "begin", record_begin)
        try:
            with pytest.raises(TypeError, match="caller_owner"):
                repo.recover_expired_leases(
                    caller_owner="different-leader-identity",
                    coordination_token=token,
                )
        finally:
            event.remove(db.engine, "begin", record_begin)

        assert transactions == [], "caller-supplied recovery identity must be refused before BEGIN"
        assert _lease_recovery_mutation_snapshot(db) == before


class TestStaleTokenFenceRefusals:
    """Every fenced verb refuses a stale epoch: raise + event + zero mutation."""

    def test_complete_run_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        factory = RecorderFactory(db)
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=token)
        with db.engine.connect() as conn:
            status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == RUN_ID)).scalar_one()
        assert status == RunStatus.RUNNING.value, "a deposed leader must not stamp a terminal status"
        assert len(_fence_refusals(db, "complete_run")) == 1

    def test_update_run_status_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        factory = RecorderFactory(db)
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            factory.run_lifecycle.update_run_status(RunStatus.FAILED, coordination_token=token)
        with db.engine.connect() as conn:
            status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == RUN_ID)).scalar_one()
        assert status == RunStatus.RUNNING.value
        assert len(_fence_refusals(db, "update_run_status")) == 1

    @pytest.mark.parametrize(
        ("verb", "call"),
        (
            pytest.param(
                "record_source_field_resolution",
                lambda lifecycle, token: lifecycle.record_source_field_resolution({"a": "a"}, "v1", coordination_token=token),
                id="record_source_field_resolution",
            ),
            pytest.param(
                "record_run_source",
                lambda lifecycle, token: lifecycle.record_run_source(
                    source_node_id=SOURCE_NODE_ID,
                    source_name="source",
                    plugin_name="test",
                    config_hash="cfg",
                    lifecycle_state="ready",
                    coordination_token=token,
                ),
                id="record_run_source",
            ),
            pytest.param(
                "update_run_source_contract",
                lambda lifecycle, token: lifecycle.update_run_source_contract(
                    source_node_id=SOURCE_NODE_ID,
                    schema_contract=SchemaContract(mode="OBSERVED", fields=(), locked=True),
                    coordination_token=token,
                ),
                id="update_run_source_contract",
            ),
            pytest.param(
                "record_secret_resolutions",
                lambda lifecycle, token: lifecycle.record_secret_resolutions(
                    [
                        SecretResolutionInput(
                            timestamp=NOW.timestamp(),
                            env_var_name="KEY",
                            source="env",
                            vault_url=None,
                            secret_name=None,
                            fingerprint="0" * 64,
                            resolution_latency_ms=1,
                        )
                    ],
                    coordination_token=token,
                ),
                id="record_secret_resolutions",
            ),
            pytest.param(
                "record_preflight_results",
                lambda lifecycle, token: lifecycle.record_preflight_results(
                    PreflightResult(
                        dependency_runs=(),
                        gate_results=(CommencementGateResult(name="gate", condition="x", result=True, context_snapshot={}),),
                    ),
                    coordination_token=token,
                ),
                id="record_preflight_results",
            ),
            pytest.param(
                "record_readiness_check",
                lambda lifecycle, token: lifecycle.record_readiness_check(
                    name="probe", collection="docs", reachable=True, count=1, message="ok", coordination_token=token
                ),
                id="record_readiness_check",
            ),
            pytest.param(
                "set_export_status",
                lambda lifecycle, token: lifecycle.set_export_status(ExportStatus.PENDING, coordination_token=token),
                id="set_export_status",
            ),
            pytest.param(
                "set_export_failed_unless_completed",
                lambda lifecycle, token: lifecycle.set_export_failed_unless_completed(error="boom", coordination_token=token),
                id="set_export_failed_unless_completed",
            ),
            pytest.param(
                "set_export_pending_unless_completed",
                lambda lifecycle, token: lifecycle.set_export_pending_unless_completed(coordination_token=token),
                id="set_export_pending_unless_completed",
            ),
        ),
    )
    def test_run_lifecycle_verb_refused(self, db: LandscapeDB, token: CoordinationToken, verb: str, call: Any) -> None:
        """ADR-048 D8.1: every RunLifecycleRepository writer fences FIRST — a deposed leader writes nothing."""
        factory = RecorderFactory(db)
        before = _run_lifecycle_snapshot(db)
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError) as raised:
            call(factory.run_lifecycle, token)
        assert raised.value.verb == verb
        assert _run_lifecycle_snapshot(db) == before, "a deposed leader must leave every run-lifecycle table untouched"
        assert len(_fence_refusals(db, verb)) == 1

    def test_create_checkpoint_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        manager = CheckpointManager(db)
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            manager.create_checkpoint(
                draft=_checkpoint_draft(1),
                coordination_token=token,
            )
        with db.engine.connect() as conn:
            count = len(conn.execute(select(checkpoints_table.c.checkpoint_id)).all())
        assert count == 0, "the refused checkpoint INSERT must roll back with the fence"
        assert len(_fence_refusals(db, "create_checkpoint")) == 1

    def test_delete_checkpoints_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        manager = CheckpointManager(db)
        manager.create_checkpoint(
            draft=_checkpoint_draft(0),
            coordination_token=token,
        )
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            manager.delete_checkpoints(RUN_ID, coordination_token=token)
        with db.engine.connect() as conn:
            count = len(conn.execute(select(checkpoints_table.c.checkpoint_id)).all())
        assert count == 1, "a deposed leader must not destroy the new leader's resume anchors"
        assert len(_fence_refusals(db, "delete_checkpoints")) == 1

    def test_complete_barrier_strict_arm_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner=WORKER)
        repo.mark_blocked(work_item_id=work_item_id, queue_key=None, barrier_key="b1", expected_lease_owner=WORKER)
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            repo.complete_barrier(
                run_id=RUN_ID,
                barrier_key="b1",
                consumed_token_ids=(token_id,),
                emitted_pending_sink=(),
                emitted_ready=(),
                coordination_token=token,
            )
        assert _work_item_row(db, token_id)["status"] == TokenWorkStatus.BLOCKED.value, "refusal before any journal mutation"
        assert len(_fence_refusals(db, "complete_barrier")) == 1

    def test_complete_barrier_legacy_wrapper_arm_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner=WORKER)
        repo.mark_blocked(work_item_id=work_item_id, queue_key=None, barrier_key="b1", expected_lease_owner=WORKER)
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            repo.mark_blocked_barrier_terminal(
                run_id=RUN_ID,
                barrier_key="b1",
                token_ids=(token_id,),
                coordination_token=token,
            )
        assert _work_item_row(db, token_id)["status"] == TokenWorkStatus.BLOCKED.value
        assert len(_fence_refusals(db, "complete_barrier")) == 1

    def test_pending_sink_barrier_wrapper_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner=WORKER)
        repo.mark_blocked(work_item_id=work_item_id, queue_key=None, barrier_key="b1", expected_lease_owner=WORKER)
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            repo.mark_blocked_barrier_pending_sink_many(
                run_id=RUN_ID,
                barrier_key="b1",
                handoffs={
                    token_id: BlockedPendingSinkHandoff(
                        row_payload_json=_payload_json(),
                        sink_name="sink-a",
                        outcome="success",
                        path="completed",
                        error_hash=None,
                        error_message=None,
                    )
                },
                coordination_token=token,
            )
        assert _work_item_row(db, token_id)["status"] == TokenWorkStatus.BLOCKED.value
        assert len(_fence_refusals(db, "complete_barrier")) == 1

    def test_reset_adoption_marker_to_pending_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """elspeth-ee18e446ff: the §E.3 crash-window reset is fenced, not CAS-implied.

        The two-takeover window this refuses: WE took the seat at this epoch and
        adopted the row, then stalled inside ``restore_from_journal``; our lease
        lapsed and a successor took over and is adopting. Our own takeover CAS
        committed before any of that and gates nothing — only the fence can stop
        the in-flight reset from clearing the successor's markers.
        """
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner=WORKER)
        repo.mark_blocked(work_item_id=work_item_id, queue_key=None, barrier_key="b1", expected_lease_owner=WORKER)
        adoption = repo.adopt_blocked_barrier_item(
            run_id=RUN_ID,
            work_item_id=work_item_id,
            token_id=token_id,
            barrier_key="b1",
            membership=None,
            buffered_outcome=None,
            coordination_token=token,
        )
        assert adoption.barrier_adopted_epoch == token.leader_epoch, "the marker this reset would clear must really be set"
        adopted_row = _work_item_row(db, token_id)
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            repo.reset_adoption_marker_to_pending(work_item_ids=[work_item_id], coordination_token=token)
        assert _work_item_row(db, token_id) == adopted_row, "a deposed leader must not reset a live successor's adoption marker"
        assert len(_fence_refusals(db, "reset_adoption_marker_to_pending")) == 1

    def test_recover_expired_leases_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner="peer-worker")
        expire_lease(db.engine, work_item_id)  # the peer lease is expired: only the fence can refuse
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            repo.recover_expired_leases(
                coordination_token=token,
            )
        row = _work_item_row(db, token_id)
        assert row["status"] == TokenWorkStatus.LEASED.value, "a deposed leader cannot rotate attempts under the new one"
        assert row["lease_owner"] == "peer-worker"
        assert len(_fence_refusals(db, "recover_expired_leases")) == 1

    def test_terminalize_pending_sinks_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner=WORKER)
        repo.mark_pending_sink(
            work_item_id=work_item_id,
            row_payload_json=_payload_json(),
            sink_name="sink-a",
            outcome="success",
            path="default_flow",
            error_hash=None,
            error_message=None,
            expected_lease_owner=WORKER,
        )
        # Durable terminal outcome witness: without the fence this row WOULD
        # be repaired — proving the refusal is the fence, not a missing match.
        with db.engine.begin() as conn:
            conn.execute(
                insert(token_outcomes_table).values(
                    outcome_id="outcome-1",
                    run_id=RUN_ID,
                    token_id=token_id,
                    outcome="success",
                    path="default_flow",
                    completed=1,
                    recorded_at=NOW,
                    sink_name="sink-a",
                )
            )
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            repo.terminalize_pending_sinks_with_terminal_outcomes(
                run_id=RUN_ID,
                caller_owner=WORKER,
                coordination_token=token,
            )
        assert _work_item_row(db, token_id)["status"] == TokenWorkStatus.PENDING_SINK.value
        assert len(_fence_refusals(db, "terminalize_pending_sinks_with_terminal_outcomes")) == 1

    def test_mark_pending_sink_terminal_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """§C.4 row 7: the epoch fence sits ON TOP of the strict owner CAS —
        a matching owner with a stale epoch is still refused."""
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner=WORKER)
        repo.mark_pending_sink(
            work_item_id=work_item_id,
            row_payload_json=_payload_json(),
            sink_name="sink-a",
            outcome="success",
            path="default_flow",
            error_hash=None,
            error_message=None,
            expected_lease_owner=WORKER,
        )
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            repo.mark_pending_sink_terminal(
                run_id=RUN_ID,
                token_id=token_id,
                expected_lease_owner=WORKER,
                coordination_token=token,
            )
        assert _work_item_row(db, token_id)["status"] == TokenWorkStatus.PENDING_SINK.value
        assert len(_fence_refusals(db, "mark_pending_sink_terminal")) == 1

    def test_mark_pending_sink_terminal_many_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner=WORKER)
        repo.mark_pending_sink(
            work_item_id=work_item_id,
            row_payload_json=_payload_json(),
            sink_name="sink-a",
            outcome="success",
            path="default_flow",
            error_hash=None,
            error_message=None,
            expected_lease_owner=WORKER,
        )
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            repo.mark_pending_sink_terminal_many(
                run_id=RUN_ID,
                token_ids=(token_id,),
                expected_lease_owner=WORKER,
                coordination_token=token,
            )
        assert _work_item_row(db, token_id)["status"] == TokenWorkStatus.PENDING_SINK.value
        assert len(_fence_refusals(db, "mark_pending_sink_terminal_many")) == 1

    def test_ingest_woken_mid_ingest_atomic_rollback(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """§C.4 row 9: a deposed leader woken mid-ingest leaves NO orphan rows row."""
        repo = TokenSchedulerRepository(db.engine)
        data_flow = RecorderFactory(db).data_flow
        _bump_epoch(db)

        def insert_row_and_token(conn):  # type: ignore[no-untyped-def]
            return data_flow.insert_row_with_token_on(
                conn,
                run_id=RUN_ID,
                source_node_id=SOURCE_NODE_ID,
                row_index=0,
                data={"id": 1},
                source_row_index=0,
                ingest_sequence=0,
                row_id="row-ingest",
                token_id="token-ingest",
            )

        with pytest.raises(RunLeadershipLostError):
            repo.ingest_row_with_initial_claim(
                coordination_token=token,
                insert_row_and_token=insert_row_and_token,
                token_id="token-ingest",
                row_id="row-ingest",
                node_id=NODE_ID,
                step_index=1,
                ingest_sequence=0,
                row_payload_json=_payload_json(),
                lease_owner=WORKER,
                lease_seconds=60,
            )
        with db.engine.connect() as conn:
            rows = conn.execute(select(rows_table.c.row_id)).scalars().all()
            items = conn.execute(select(token_work_items_table.c.work_item_id)).scalars().all()
        assert rows == [], "the rows insert rolls back with everything else — no orphan rows row exists"
        assert items == []
        assert len(_fence_refusals(db, "ingest_row_with_initial_claim")) == 1

    def test_fenced_create_row_with_token_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        data_flow = RecorderFactory(db).data_flow
        _bump_epoch(db)
        with pytest.raises(RunLeadershipLostError):
            data_flow.create_row_with_token(
                RUN_ID,
                SOURCE_NODE_ID,
                0,
                {"id": 1},
                source_row_index=0,
                ingest_sequence=0,
                coordination_token=token,
            )
        with db.engine.connect() as conn:
            rows = conn.execute(select(rows_table.c.row_id)).scalars().all()
        assert rows == []
        assert len(_fence_refusals(db, "create_row_with_token")) == 1

    def test_release_seat_refused(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """The seat is a RUN-scoped row, so vacating it is a LEADER write (ADR-030 D4).

        A deposed leader's teardown must not vacate the seat its usurper now
        holds. The refusal is swallowed at the verb (release is a ``finally``
        arm where a second leadership-lost signal would mask the first), so
        the contract here is zero mutation plus the fence's own evidence.
        """
        repo = RunCoordinationRepository(db.engine)
        _bump_epoch(db)
        seat_before = _seat_image(db)
        worker_before = _worker_image(db, WORKER)

        repo.release_seat(token=token)  # declared no-op, never raises

        assert _seat_image(db) == seat_before, "a deposed leader must not vacate the usurper's seat"
        assert _worker_image(db, WORKER) == worker_before
        refusals = _fence_refusals(db, "release_seat")
        assert len(refusals) == 1
        assert refusals[0]["leader_epoch"] == token.leader_epoch


class TestStaleMembershipTokenFenceRefusals:
    """ADR-030 D4's SECOND fence: a departed or evicted member is refused.

    The member-scoped analogue of :class:`TestStaleTokenFenceRefusals`. The
    stale authority here is a :class:`WorkerMembershipToken` whose
    ``run_workers`` row left ``active`` — the single-use identity doctrine
    means it never returns — and the contract is the same three parts:
    the refusal, exactly one ``fence_refusal`` event naming the verb and the
    membership fence, and ZERO payload mutation because
    :func:`verify_membership_fence` is the FIRST statement of the verb's
    IMMEDIATE transaction.

    Both member-fenced verbs are teardown/liveness writes that REIFY the
    refusal as a declared outcome rather than propagating it
    (``worker_active=False``; the idempotent departure no-op), so the raise
    itself is pinned once on the helper.
    """

    def test_fenced_member_transaction_raises_and_rolls_back(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """The helper's own contract: the payload never runs, one refusal event."""
        member = WorkerMembershipToken(run_id=RUN_ID, worker_id=WORKER)
        _depart_member(db, WORKER)
        seat_before = _seat_image(db)

        with (
            pytest.raises(RunMembershipLostError) as excinfo,
            fenced_member_transaction(db.engine, member_token=member, verb="probe") as conn,
        ):
            conn.execute(update(run_coordination_table).values(leader_worker_id="usurper"))

        assert (excinfo.value.run_id, excinfo.value.worker_id, excinfo.value.verb) == (RUN_ID, WORKER, "probe")
        assert _seat_image(db) == seat_before, "the payload rolled back with the fence"
        refusals = _fence_refusals(db, "probe")
        assert len(refusals) == 1
        assert refusals[0]["leader_epoch"] is None, "a member holds no epoch"
        assert json.loads(str(refusals[0]["context_json"]))["fence"] == "membership"

    def test_worker_heartbeat_refused_for_evicted_member(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """A zombie beat cannot revive membership, and cannot extend the seat."""
        repo = RunCoordinationRepository(db.engine)
        member = WorkerMembershipToken(run_id=RUN_ID, worker_id=WORKER)
        _evict_member(db, WORKER)
        seat_before = _seat_image(db)
        worker_before = _worker_image(db, WORKER)

        snapshot = repo.worker_heartbeat(member_token=member, window_seconds=80.0)

        assert snapshot.worker_active is False, "the refusal IS the declared coordination-lost outcome"
        assert _worker_image(db, WORKER) == worker_before, "an evicted row never returns to active"
        assert _seat_image(db) == seat_before, "a refused beat must not extend the seat it no longer holds"
        assert len(_fence_refusals(db, "worker_heartbeat")) == 1

    def test_depart_worker_refused_for_departed_member(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """The second departure writes nothing and emits no second ``worker_depart``."""
        repo = RunCoordinationRepository(db.engine)
        member = WorkerMembershipToken(run_id=RUN_ID, worker_id=WORKER)
        _depart_member(db, WORKER)
        worker_before = _worker_image(db, WORKER)

        repo.depart_worker(member_token=member)  # declared idempotent no-op

        assert _worker_image(db, WORKER) == worker_before
        with db.engine.connect() as conn:
            departs = conn.execute(
                select(run_coordination_events_table.c.event_id)
                .where(run_coordination_events_table.c.run_id == RUN_ID)
                .where(run_coordination_events_table.c.event_type == "worker_depart")
            ).all()
        assert departs == [], "the fence refused before the departure CAS could write its event"
        assert len(_fence_refusals(db, "depart_worker")) == 1

    def test_member_fence_admits_an_active_member(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """The positive arm: an active row passes and the payload commits.

        Without this, every arm above would still pass if the fence refused
        unconditionally — the failure mode that turns a fence into an outage.
        """
        repo = RunCoordinationRepository(db.engine)
        member = WorkerMembershipToken(run_id=RUN_ID, worker_id=WORKER)

        snapshot = repo.worker_heartbeat(member_token=member, window_seconds=80.0)

        assert snapshot.worker_active is True
        assert snapshot.worker_role == "leader", "the leader IS a member (CoordinationToken.membership)"
        assert _fence_refusals(db, "worker_heartbeat") == []


class TestStrictPendingSinkOwnerCAS:
    """Strict owner CAS on pending-sink terminalization.

    These arms exercise the OWNER CAS in isolation (no ``coordination_token``
    → the unfenced legacy arm). The verbs ALSO carry the §C.4 row-7
    per-terminalization-batch epoch fence when a token is threaded — the
    fenced refusals are pinned in :class:`TestStaleTokenFenceRefusals` below
    and e2e in tests/e2e/recovery/test_suspended_winner_fences.py.
    """

    def test_expected_lease_owner_is_a_required_keyword(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """The strictness signature pin (design :449): ``expected_lease_owner``
        is keyword-only and REQUIRED on both verbs — omission is a TypeError,
        not a silent owner-blind terminalization."""
        repo, token_id = self._parked_handoff(db, owner=WORKER)
        with pytest.raises(TypeError, match="expected_lease_owner"):
            repo.mark_pending_sink_terminal(run_id=RUN_ID, token_id=token_id)  # type: ignore[call-arg]
        with pytest.raises(TypeError, match="expected_lease_owner"):
            repo.mark_pending_sink_terminal_many(run_id=RUN_ID, token_ids=(token_id,))  # type: ignore[call-arg]
        assert _work_item_row(db, token_id)["status"] == TokenWorkStatus.PENDING_SINK.value

    def _parked_handoff(self, db: LandscapeDB, *, owner: str) -> tuple[TokenSchedulerRepository, str]:
        repo = TokenSchedulerRepository(db.engine)
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=0, owner=owner)
        repo.mark_pending_sink(
            work_item_id=work_item_id,
            row_payload_json=_payload_json(),
            sink_name="sink-a",
            outcome="success",
            path="default_flow",
            error_hash=None,
            error_message=None,
            expected_lease_owner=owner,
        )
        return repo, token_id

    def test_attributed_park_keeps_owner_without_lease(self, db: LandscapeDB, token: CoordinationToken) -> None:
        _repo, token_id = self._parked_handoff(db, owner=WORKER)
        row = _work_item_row(db, token_id)
        assert row["status"] == TokenWorkStatus.PENDING_SINK.value
        assert row["lease_owner"] == WORKER, "parked, owner-attributed"
        assert row["lease_expires_at"] is None, "…but not leased"

    def test_owner_mismatch_refuses_with_zero_mutation(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo, token_id = self._parked_handoff(db, owner=WORKER)
        # Epoch fence passes (valid token), owner CAS refuses (wrong owner) → 0.
        terminalized = repo.mark_pending_sink_terminal(
            run_id=RUN_ID, token_id=token_id, expected_lease_owner="some-other-worker", coordination_token=token
        )
        assert terminalized == 0
        row = _work_item_row(db, token_id)
        assert row["status"] == TokenWorkStatus.PENDING_SINK.value
        assert row["lease_owner"] == WORKER

    def test_null_owner_acceptance_is_removed(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """A NULL-parked handoff (the reap arm's park) no longer terminalizes."""
        repo, token_id = self._parked_handoff(db, owner=WORKER)
        with db.engine.begin() as conn:
            conn.execute(update(token_work_items_table).where(token_work_items_table.c.token_id == token_id).values(lease_owner=None))
        terminalized = repo.mark_pending_sink_terminal(
            run_id=RUN_ID, token_id=token_id, expected_lease_owner=WORKER, coordination_token=token
        )
        assert terminalized == 0, "the historical NULL-owner acceptance arm is deleted"
        assert _work_item_row(db, token_id)["status"] == TokenWorkStatus.PENDING_SINK.value

    def test_matching_owner_terminalizes(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo, token_id = self._parked_handoff(db, owner=WORKER)
        terminalized = repo.mark_pending_sink_terminal(
            run_id=RUN_ID, token_id=token_id, expected_lease_owner=WORKER, coordination_token=token
        )
        assert terminalized == 1
        assert _work_item_row(db, token_id)["status"] == TokenWorkStatus.TERMINAL.value

    def test_many_owner_mismatch_refuses_batch(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo, token_id = self._parked_handoff(db, owner=WORKER)
        with pytest.raises(AuditIntegrityError, match="strict owner CAS"):
            repo.mark_pending_sink_terminal_many(
                run_id=RUN_ID, token_ids=(token_id,), expected_lease_owner="some-other-worker", coordination_token=token
            )
        assert _work_item_row(db, token_id)["status"] == TokenWorkStatus.PENDING_SINK.value

    def test_many_null_park_refuses_batch(self, db: LandscapeDB, token: CoordinationToken) -> None:
        repo, token_id = self._parked_handoff(db, owner=WORKER)
        with db.engine.begin() as conn:
            conn.execute(update(token_work_items_table).where(token_work_items_table.c.token_id == token_id).values(lease_owner=None))
        with pytest.raises(AuditIntegrityError, match="strict owner CAS"):
            repo.mark_pending_sink_terminal_many(
                run_id=RUN_ID, token_ids=(token_id,), expected_lease_owner=WORKER, coordination_token=token
            )

    def test_reclaim_restores_attribution_for_reaped_handoff(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """The reap arm parks NULL; claim_pending_sink restores attribution."""
        repo, token_id = self._parked_handoff(db, owner=WORKER)
        with db.engine.begin() as conn:
            conn.execute(update(token_work_items_table).where(token_work_items_table.c.token_id == token_id).values(lease_owner=None))
        _ensure_active_worker(db, "resume-worker")
        reclaimed = repo.claim_pending_sink(run_id=RUN_ID, lease_owner="resume-worker", lease_seconds=60)
        assert reclaimed is not None and reclaimed.token_id == token_id
        terminalized = repo.mark_pending_sink_terminal(
            run_id=RUN_ID, token_id=token_id, expected_lease_owner="resume-worker", coordination_token=token
        )
        assert terminalized == 1


class TestValidTokenFenceSemantics:
    """The fence's positive contract: extend-on-verify and §D quiescence."""

    def test_fenced_verb_extends_the_seat(self, db: LandscapeDB, token: CoordinationToken) -> None:
        manager = CheckpointManager(db)
        # Pin the seat expiry to the distant past so the extension is
        # observable regardless of the wall clock (the manager fences with
        # datetime.now(UTC), not the fixture clock).
        before = datetime(2020, 1, 1, tzinfo=UTC)
        with db.engine.begin() as conn:
            conn.execute(
                update(run_coordination_table).where(run_coordination_table.c.run_id == RUN_ID).values(leader_heartbeat_expires_at=before)
            )
        manager.create_checkpoint(
            draft=_checkpoint_draft(1),
            coordination_token=token,
        )
        with db.engine.connect() as conn:
            after = conn.execute(
                select(run_coordination_table.c.leader_heartbeat_expires_at).where(run_coordination_table.c.run_id == RUN_ID)
            ).scalar_one()
        assert after.replace(tzinfo=UTC) > before, "every fenced verb doubles as the seat heartbeat (verify-AND-EXTEND)"
        assert _fence_refusals(db, "create_checkpoint") == []

    def _adopt_blocked_row(
        self, db: LandscapeDB, repo: TokenSchedulerRepository, *, sequence: int, token: CoordinationToken
    ) -> tuple[str, str]:
        """BLOCKED barrier hold adopted through the real fenced verb; returns (token_id, work_item_id)."""
        token_id, _row_id, work_item_id = _enqueue_and_claim(db, repo, sequence=sequence, owner=WORKER)
        repo.mark_blocked(work_item_id=work_item_id, queue_key=None, barrier_key=f"b{sequence}", expected_lease_owner=WORKER)
        adoption = repo.adopt_blocked_barrier_item(
            run_id=RUN_ID,
            work_item_id=work_item_id,
            token_id=token_id,
            barrier_key=f"b{sequence}",
            membership=None,
            buffered_outcome=None,
            coordination_token=token,
        )
        assert adoption.barrier_adopted_epoch == token.leader_epoch
        return token_id, work_item_id

    def test_reset_adoption_marker_to_pending_resets_only_blocked_rows(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """Behaviour preservation: adding the fence must not change WHICH rows the verb resets.

        A fence that also changes the write is two changes wearing one commit,
        so the live-token arm pins the selection, not merely that something
        happened. Three adopted rows, all three ids passed, ONLY the two still
        BLOCKED are reset; the terminal row keeps its marker (§E.4 treats
        adopted-at-any-epoch rows as restorable members).
        """
        repo = TokenSchedulerRepository(db.engine)
        blocked: list[tuple[str, str]] = []
        for sequence in (0, 1):
            token_id, work_item_id = self._adopt_blocked_row(db, repo, sequence=sequence, token=token)
            blocked.append((token_id, work_item_id))
        terminal_token_id, terminal_work_item_id = self._adopt_blocked_row(db, repo, sequence=2, token=token)
        with db.engine.begin() as conn:
            conn.execute(
                update(token_work_items_table)
                .where(token_work_items_table.c.work_item_id == terminal_work_item_id)
                .values(status=TokenWorkStatus.TERMINAL.value)
            )
        adopted_epoch = token.leader_epoch
        assert _work_item_row(db, terminal_token_id)["barrier_adopted_epoch"] == adopted_epoch

        reset = repo.reset_adoption_marker_to_pending(
            work_item_ids=[work_item_id for _token_id, work_item_id in blocked] + [terminal_work_item_id],
            coordination_token=token,
        )

        assert reset == 2, "only the BLOCKED rows are reset, even though three ids were passed"
        for token_id, _work_item_id in blocked:
            assert _work_item_row(db, token_id)["barrier_adopted_epoch"] is None, "the BLOCKED row is intake-pending again"
        assert _work_item_row(db, terminal_token_id)["barrier_adopted_epoch"] == adopted_epoch, (
            "a non-BLOCKED row keeps its adoption marker: the fence must not widen the write set"
        )
        assert _fence_refusals(db, "reset_adoption_marker_to_pending") == []

    def test_reset_adoption_marker_to_pending_empty_ids_opens_no_transaction(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """No ids means no database effect, so the early return precedes the fence entirely.

        Even a deposed leader is not refused here: there is nothing to refuse.
        """
        repo = TokenSchedulerRepository(db.engine)
        _bump_epoch(db)
        before = _barrier_mutation_snapshot(db)
        transactions: list[object] = []

        def record_begin(conn: object) -> None:
            transactions.append(conn)

        event.listen(db.engine, "begin", record_begin)
        try:
            assert repo.reset_adoption_marker_to_pending(work_item_ids=[], coordination_token=token) == 0
        finally:
            event.remove(db.engine, "begin", record_begin)

        assert transactions == [], "an empty reset must return before opening a transaction"
        assert _fence_refusals(db, "reset_adoption_marker_to_pending") == []
        assert _barrier_mutation_snapshot(db) == before

    def test_complete_run_quiescence_predicate_refuses_residual_work(self, db: LandscapeDB, token: CoordinationToken) -> None:
        """§D: a SUCCESS finalize over residual scheduler work is refused in-statement."""
        repo = TokenSchedulerRepository(db.engine)
        token_id, row_id = _seed_row_and_token(db, sequence=0)
        repo.enqueue_ready(
            run_id=RUN_ID,
            token_id=token_id,
            row_id=row_id,
            node_id=NODE_ID,
            step_index=1,
            ingest_sequence=0,
            row_payload_json=_payload_json(),
        )
        factory = RecorderFactory(db)
        with pytest.raises(OrchestrationInvariantError, match="residual scheduler work"):
            factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=token)
        with db.engine.connect() as conn:
            status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == RUN_ID)).scalar_one()
        assert status == RunStatus.RUNNING.value
        # FAILED is exempt from quiescence: the journal stays intact for resume.
        run = factory.run_lifecycle.complete_run(RunStatus.FAILED, coordination_token=token)
        assert run.status == RunStatus.FAILED

    def test_complete_run_writes_finalize_event(self, db: LandscapeDB, token: CoordinationToken) -> None:
        factory = RecorderFactory(db)
        factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=token)
        with db.engine.connect() as conn:
            events = (
                conn.execute(
                    select(run_coordination_events_table)
                    .where(run_coordination_events_table.c.run_id == RUN_ID)
                    .where(run_coordination_events_table.c.event_type == "finalize")
                )
                .mappings()
                .all()
            )
        assert len(events) == 1
        assert events[0]["worker_id"] == WORKER
        assert events[0]["leader_epoch"] == token.leader_epoch
        assert json.loads(str(events[0]["context_json"]))["status"] == RunStatus.COMPLETED.value
