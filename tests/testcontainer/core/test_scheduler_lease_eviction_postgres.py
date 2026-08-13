"""PostgreSQL serialization proof: membership fencing vs worker eviction.

Ticket elspeth-6903f82511. The membership fence rides the claim/heartbeat CAS
UPDATE as an EXISTS predicate over ``run_workers``; MVCC predicate reads take
no row locks, so before the fix a fenced claim or renewal could commit around
an in-flight eviction that had already observed no live lease — leaving an
evicted worker holding a live lease.  These tests drive the exact
interleavings on a real PostgreSQL backend:

* an eviction paused with its registry UPDATE uncommitted must BLOCK a fenced
  claim / heartbeat renewal, which then observes the committed eviction and
  is refused with ``RunWorkerEvictedError`` (no lease granted or renewed);
* an in-flight fenced claim (shared membership lock held, lease uncommitted)
  must BLOCK ``evict_worker``, whose live-lease precondition then sees the
  committed lease and returns ``False`` (no eviction).

Interleavings are event-fenced (pause hooks on cursor execution); the only
polling is a bounded wait on ``pg_stat_activity`` for the deterministic
"blocked on a row lock" state — never a timing assumption.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from scripts.state_engine_profile_reporter import RuntimeProfileReporter
from sqlalchemy import event, insert, select
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
from tests.helpers.state_engine import capture_state_engine_image

from elspeth.contracts import TerminalOutcome, TerminalPath
from elspeth.contracts.coordination import (
    DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
    CoordinationToken,
)
from elspeth.contracts.errors import RunWorkerEvictedError, SchedulerLeaseLostError
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkItem, TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    nodes_table,
    rows_table,
    run_coordination_table,
    run_workers_table,
    runs_table,
    scheduler_events_table,
    token_work_items_table,
    tokens_table,
)

pytestmark = pytest.mark.testcontainer

WINDOW = DEFAULT_RUN_LIVENESS_WINDOW_SECONDS
GRACE = DEFAULT_RUN_LIVENESS_WINDOW_SECONDS


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()


def _seed(
    engine: Any,
    *,
    run_id: str,
    leader_id: str,
    worker_id: str | None,
    worker_heartbeat_expires_at: datetime | None,
    now: datetime,
    leader_window_seconds: float = WINDOW,
) -> CoordinationToken:
    with engine.begin() as conn:
        conn.execute(
            insert(runs_table).values(
                run_id=run_id,
                started_at=now,
                config_hash="config",
                settings_json="{}",
                canonical_version="v1",
                status="running",
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        )
        for node_id, node_type, plugin in (("source-a", "source", "csv"), ("transform-1", "transform", "identity")):
            conn.execute(
                insert(nodes_table).values(
                    run_id=run_id,
                    node_id=node_id,
                    plugin_name=plugin,
                    node_type=node_type,
                    plugin_version="1.0",
                    determinism="deterministic",
                    config_hash="config",
                    config_json="{}",
                    registered_at=now,
                )
            )
        conn.execute(
            insert(run_coordination_table).values(
                run_id=run_id,
                leader_worker_id=leader_id,
                leader_epoch=1,
                leader_heartbeat_expires_at=now + timedelta(seconds=leader_window_seconds),
                updated_at=now,
            )
        )
        conn.execute(
            insert(run_workers_table).values(
                worker_id=leader_id,
                run_id=run_id,
                role="leader",
                status="active",
                registered_at=now,
                heartbeat_expires_at=now + timedelta(seconds=leader_window_seconds),
            )
        )
        if worker_id is not None:
            assert worker_heartbeat_expires_at is not None
            conn.execute(
                insert(run_workers_table).values(
                    worker_id=worker_id,
                    run_id=run_id,
                    role="follower",
                    status="active",
                    registered_at=now,
                    heartbeat_expires_at=worker_heartbeat_expires_at,
                )
            )
    return CoordinationToken(run_id=run_id, worker_id=leader_id, leader_epoch=1)


def _enqueue_ready_item(
    engine: Any,
    *,
    run_id: str,
    token_id: str,
    now: datetime,
    join_group_id: str | None = None,
) -> TokenWorkItem:
    row_id = f"row-{token_id}"
    with engine.begin() as conn:
        conn.execute(
            insert(rows_table).values(
                row_id=row_id,
                run_id=run_id,
                source_node_id="source-a",
                row_index=0,
                source_row_index=0,
                ingest_sequence=0,
                source_data_hash=f"hash-{token_id}",
                created_at=now,
            )
        )
        conn.execute(insert(tokens_table).values(token_id=token_id, row_id=row_id, run_id=run_id, created_at=now))
    repo = TokenSchedulerRepository(engine)
    payload = TokenSchedulerRepository.serialize_row_payload(
        PipelineRow({"id": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
    )
    return repo.enqueue_ready(
        run_id=run_id,
        token_id=token_id,
        row_id=row_id,
        node_id="transform-1",
        step_index=1,
        ingest_sequence=0,
        row_payload_json=payload,
        available_at=now,
        join_group_id=join_group_id,
    )


def _scheduler_event_types(db: LandscapeDB, *, run_id: str) -> list[str]:
    with db.read_only_connection() as conn:
        return list(
            conn.execute(
                select(scheduler_events_table.c.event_type)
                .where(scheduler_events_table.c.run_id == run_id)
                .order_by(scheduler_events_table.c.recorded_at, scheduler_events_table.c.event_id)
            ).scalars()
        )


def _scheduler_events(db: LandscapeDB, *, run_id: str) -> list[dict[str, object]]:
    with db.read_only_connection() as conn:
        rows = (
            conn.execute(
                select(scheduler_events_table)
                .where(scheduler_events_table.c.run_id == run_id)
                .order_by(scheduler_events_table.c.recorded_at, scheduler_events_table.c.event_id)
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _work_item_row(db: LandscapeDB, *, run_id: str) -> dict[str, object]:
    with db.read_only_connection() as conn:
        row = conn.execute(select(token_work_items_table).where(token_work_items_table.c.run_id == run_id)).mappings().one()
    return dict(row)


def _set_postgresql_transaction_timeouts(conn: Any) -> None:
    conn.exec_driver_sql("SET LOCAL statement_timeout = '15000ms'")
    conn.exec_driver_sql("SET LOCAL lock_timeout = '5000ms'")


def _run_two_contenders(
    first_db: LandscapeDB,
    first: Callable[[], object],
    second_db: LandscapeDB,
    second: Callable[[], object],
) -> tuple[object, object]:
    release_update = threading.Event()
    reached_update = {
        "first": threading.Event(),
        "second": threading.Event(),
    }
    outcomes: dict[str, object] = {}

    def pause_before_work_item_update(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _params: Any,
        _context: Any,
        _many: bool,
    ) -> None:
        name = threading.current_thread().name
        contender = {"first-contender": "first", "second-contender": "second"}.get(name)
        if contender is None or reached_update[contender].is_set():
            return
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("UPDATE TOKEN_WORK_ITEMS"):
            reached_update[contender].set()
            if not release_update.wait(timeout=15):
                raise TimeoutError(f"{contender} contender timed out at the pre-UPDATE race seam")

    def invoke(name: str, operation: Callable[[], object]) -> None:
        try:
            outcomes[name] = operation()
        except BaseException as exc:  # pragma: no cover - asserted by caller
            outcomes[name] = exc

    threads = (
        threading.Thread(target=invoke, args=("first", first), name="first-contender"),
        threading.Thread(target=invoke, args=("second", second), name="second-contender"),
    )
    engines = (first_db.engine, second_db.engine)
    for engine in engines:
        event.listen(engine, "begin", _set_postgresql_transaction_timeouts)
        event.listen(engine, "before_cursor_execute", pause_before_work_item_update)
    started: list[threading.Thread] = []
    teardown_failure: str | None = None
    try:
        for thread in threads:
            thread.start()
            started.append(thread)

        deadline = time.monotonic() + 15
        while not all(gate.is_set() for gate in reached_update.values()):
            exited_early = [name for name, gate in reached_update.items() if name in outcomes and not gate.is_set()]
            assert not exited_early, f"PostgreSQL contenders exited before the pre-UPDATE race seam: {exited_early!r}"
            if time.monotonic() >= deadline:
                missing = [name for name, gate in reached_update.items() if not gate.is_set()]
                raise AssertionError(f"PostgreSQL contenders did not reach the pre-UPDATE race seam: {missing!r}")
            time.sleep(0.01)

        release_update.set()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads), "PostgreSQL contenders did not finish within the bounded wait"
    finally:
        release_update.set()
        for thread in started:
            thread.join(timeout=20)
        alive = [thread.name for thread in started if thread.is_alive()]
        if alive:
            teardown_failure = f"PostgreSQL contender teardown remained live after bounded joins: {alive!r}"
        for engine in engines:
            event.remove(engine, "before_cursor_execute", pause_before_work_item_update)
            event.remove(engine, "begin", _set_postgresql_transaction_timeouts)
    assert teardown_failure is None, teardown_failure
    return outcomes["first"], outcomes["second"]


@pytest.mark.timeout(120)
def test_postgresql_ready_claim_conditional_update_has_one_winner(
    postgres_url: str,
    request: pytest.FixtureRequest,
) -> None:
    """Independent connections prove one conditional READY winner.

    Distinct active membership rows keep the membership locks independent so
    both contenders deterministically reach the work-item CAS. This proves
    PostgreSQL transaction semantics for the AWS single-leader profile; it
    does not enable or claim multi-replica scheduler support.
    """
    now = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    run_id = "run-ready-conditional-winner"
    first_owner = "leader-ready"
    second_owner = "worker-ready"
    with ExitStack() as resources:
        first_db = LandscapeDB.from_url(postgres_url)
        resources.callback(first_db.close)
        second_db = LandscapeDB.from_url(postgres_url)
        resources.callback(second_db.close)
        _seed(
            first_db.engine,
            run_id=run_id,
            leader_id=first_owner,
            worker_id=second_owner,
            worker_heartbeat_expires_at=now + timedelta(seconds=30),
            now=now,
            leader_window_seconds=30,
        )
        original = _enqueue_ready_item(first_db.engine, run_id=run_id, token_id="tok-ready-winner", now=now)

        if request.config.pluginmanager.hasplugin("scripts.state_engine_profile_reporter"):
            reporter = cast(RuntimeProfileReporter, request.getfixturevalue("state_engine_profile"))
            with first_db.engine.connect() as connection:
                reporter.observe_postgresql(connection, deployment="aws-single-leader-landscape")

        first_repo = TokenSchedulerRepository(first_db.engine)
        second_repo = TokenSchedulerRepository(second_db.engine)
        outcomes = _run_two_contenders(
            first_db,
            lambda: first_repo.claim_ready(run_id=run_id, lease_owner=first_owner, lease_seconds=30, now=now),
            second_db,
            lambda: second_repo.claim_ready(run_id=run_id, lease_owner=second_owner, lease_seconds=30, now=now),
        )
        winners = [outcome for outcome in outcomes if isinstance(outcome, TokenWorkItem)]
        losers = [outcome for outcome in outcomes if outcome is None]
        assert len(winners) == 1
        assert len(losers) == 1
        winner = winners[0]
        assert winner.work_item_id == original.work_item_id
        assert winner.attempt == original.attempt == 1
        assert winner.status is TokenWorkStatus.LEASED
        assert winner.lease_owner in {first_owner, second_owner}
        assert _scheduler_event_types(first_db, run_id=run_id).count(SchedulerEventType.CLAIM_READY.value) == 1


@pytest.mark.timeout(120)
def test_postgresql_pending_sink_conditional_update_preserves_exact_bundle(postgres_url: str) -> None:
    """One redrive claimant wins while the complete sink bundle stays exact."""
    now = datetime(2026, 8, 12, 2, 0, tzinfo=UTC)
    run_id = "run-pending-sink-conditional-winner"
    first_owner = "leader-pending-sink"
    second_owner = "worker-pending-sink"
    with ExitStack() as resources:
        first_db = LandscapeDB.from_url(postgres_url)
        resources.callback(first_db.close)
        second_db = LandscapeDB.from_url(postgres_url)
        resources.callback(second_db.close)
        _seed(
            first_db.engine,
            run_id=run_id,
            leader_id=first_owner,
            worker_id=second_owner,
            worker_heartbeat_expires_at=now + timedelta(seconds=30),
            now=now,
            leader_window_seconds=30,
        )
        first_repo = TokenSchedulerRepository(first_db.engine)
        second_repo = TokenSchedulerRepository(second_db.engine)
        item = _enqueue_ready_item(first_db.engine, run_id=run_id, token_id="tok-pending-winner", now=now)
        claimed = first_repo.claim_ready(run_id=run_id, lease_owner=first_owner, lease_seconds=30, now=now)
        assert claimed is not None
        pending = first_repo.mark_pending_sink(
            work_item_id=item.work_item_id,
            row_payload_json=item.row_payload_json,
            sink_name="sink-a",
            outcome="success",
            path="default_flow",
            error_hash=None,
            error_message=None,
            now=now,
            expected_lease_owner=first_owner,
            worker_id=first_owner,
        )
        exact_bundle = (
            pending.work_item_id,
            pending.attempt,
            pending.row_payload_json,
            pending.pending_sink_name,
            pending.pending_outcome,
            pending.pending_path,
            pending.pending_error_hash,
            pending.pending_error_message,
        )
        outcomes = _run_two_contenders(
            first_db,
            lambda: first_repo.claim_pending_sink(run_id=run_id, lease_owner=first_owner, lease_seconds=30, now=now),
            second_db,
            lambda: second_repo.claim_pending_sink(run_id=run_id, lease_owner=second_owner, lease_seconds=30, now=now),
        )
        winners = [outcome for outcome in outcomes if isinstance(outcome, TokenWorkItem)]
        assert len(winners) == 1
        assert sum(outcome is None for outcome in outcomes) == 1
        winner = winners[0]
        assert (
            winner.work_item_id,
            winner.attempt,
            winner.row_payload_json,
            winner.pending_sink_name,
            winner.pending_outcome,
            winner.pending_path,
            winner.pending_error_hash,
            winner.pending_error_message,
        ) == exact_bundle
        assert winner.status is TokenWorkStatus.LEASED
        assert winner.lease_owner in {first_owner, second_owner}
        assert _scheduler_event_types(first_db, run_id=run_id).count(SchedulerEventType.CLAIM_PENDING_SINK.value) == 1


@pytest.mark.timeout(120)
def test_postgresql_registered_lease_heartbeat_changes_only_expiry_and_updated_at(postgres_url: str) -> None:
    """AUX-01: a successful registered heartbeat emits no scheduler event."""
    now = datetime(2026, 8, 12, 2, 30, tzinfo=UTC)
    heartbeat_at = now + timedelta(seconds=5)
    run_id = "run-postgresql-lease-heartbeat"
    owner = "leader-lease-heartbeat"
    db = LandscapeDB.from_url(postgres_url)
    try:
        _seed(
            db.engine,
            run_id=run_id,
            leader_id=owner,
            worker_id=None,
            worker_heartbeat_expires_at=None,
            now=now,
            leader_window_seconds=30,
        )
        repo = TokenSchedulerRepository(db.engine)
        item = _enqueue_ready_item(db.engine, run_id=run_id, token_id="tok-lease-heartbeat", now=now)
        assert repo.claim_ready(run_id=run_id, lease_owner=owner, lease_seconds=10, now=now) is not None
        before = _work_item_row(db, run_id=run_id)
        events_before = _scheduler_events(db, run_id=run_id)

        new_expiry = repo.heartbeat_lease(
            run_id=run_id,
            work_item_id=item.work_item_id,
            lease_owner=owner,
            lease_seconds=30,
            now=heartbeat_at,
            membership_fenced=True,
        )

        expected = dict(before)
        expected["lease_expires_at"] = heartbeat_at + timedelta(seconds=30)
        expected["updated_at"] = heartbeat_at
        assert new_expiry == expected["lease_expires_at"]
        assert _work_item_row(db, run_id=run_id) == expected
        assert _scheduler_events(db, run_id=run_id) == events_before
    finally:
        db.close()


@pytest.mark.timeout(120)
def test_postgresql_heartbeat_cas_loss_after_strict_recovery_records_only_lease_lost(postgres_url: str) -> None:
    """AUX-02: a recovered generation refuses its predecessor with evidence."""
    now = datetime(2026, 8, 12, 2, 45, tzinfo=UTC)
    lease_expiry = now + timedelta(seconds=10)
    run_id = "run-postgresql-heartbeat-cas-loss"
    original_leader = "leader-heartbeat-loss"
    lease_owner = "worker-heartbeat-loss"
    db = LandscapeDB.from_url(postgres_url)
    try:
        _seed(
            db.engine,
            run_id=run_id,
            leader_id=original_leader,
            worker_id=lease_owner,
            worker_heartbeat_expires_at=now + timedelta(seconds=5),
            now=now,
            leader_window_seconds=1,
        )
        repo = TokenSchedulerRepository(db.engine)
        coord = RunCoordinationRepository(db.engine)
        original = _enqueue_ready_item(db.engine, run_id=run_id, token_id="tok-heartbeat-cas-loss", now=now)
        claimed = repo.claim_ready(run_id=run_id, lease_owner=lease_owner, lease_seconds=10, now=now)
        assert claimed is not None
        successor = coord.acquire_run_leadership(
            run_id=run_id,
            worker_id="leader-heartbeat-loss-successor",
            now=now + timedelta(seconds=2),
            window_seconds=WINDOW,
        )
        recovered_at = lease_expiry + timedelta(microseconds=1)
        assert repo.recover_expired_leases(now=recovered_at, coordination_token=successor, grace_seconds=0) == 1
        row_before = _work_item_row(db, run_id=run_id)
        events_before = _scheduler_events(db, run_id=run_id)

        with pytest.raises(SchedulerLeaseLostError):
            repo.heartbeat_lease(
                run_id=run_id,
                work_item_id=original.work_item_id,
                lease_owner=lease_owner,
                lease_seconds=30,
                now=recovered_at + timedelta(seconds=1),
                membership_fenced=True,
            )

        assert _work_item_row(db, run_id=run_id) == row_before
        events_after = _scheduler_events(db, run_id=run_id)
        assert events_after[:-1] == events_before
        lease_lost = events_after[-1]
        assert (
            lease_lost["event_type"],
            lease_lost["work_item_id"],
            lease_lost["from_status"],
            lease_lost["to_status"],
            lease_lost["from_lease_owner"],
            lease_lost["to_lease_owner"],
            lease_lost["from_attempt"],
            lease_lost["to_attempt"],
            lease_lost["caller_owner"],
        ) == (
            SchedulerEventType.LEASE_LOST.value,
            original.work_item_id,
            TokenWorkStatus.LEASED.value,
            TokenWorkStatus.READY.value,
            lease_owner,
            None,
            original.attempt,
            original.attempt + 1,
            lease_owner,
        )
        assert lease_lost["recorded_at"] == recovered_at + timedelta(seconds=1)
        assert '"reason":"heartbeat_cas_miss_after_recovery"' in str(lease_lost["context_json"])
        assert _scheduler_event_types(db, run_id=run_id)[-1] == SchedulerEventType.LEASE_LOST.value
    finally:
        db.close()


@pytest.mark.timeout(120)
def test_postgresql_transform_recovery_excludes_expiry_equality_then_rotates_once(postgres_url: str) -> None:
    now = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)
    lease_expiry = now + timedelta(seconds=10)
    run_id = "run-transform-recovery-boundary"
    original_leader = "leader-transform-original"
    db = LandscapeDB.from_url(postgres_url)
    original_token = _seed(
        db.engine,
        run_id=run_id,
        leader_id=original_leader,
        worker_id=None,
        worker_heartbeat_expires_at=None,
        now=now,
        leader_window_seconds=1,
    )
    repo = TokenSchedulerRepository(db.engine)
    coord = RunCoordinationRepository(db.engine)
    original = _enqueue_ready_item(db.engine, run_id=run_id, token_id="tok-transform-recovery", now=now)
    claimed = repo.claim_ready(run_id=run_id, lease_owner=original_token.worker_id, lease_seconds=10, now=now)
    assert claimed is not None and claimed.lease_expires_at == lease_expiry
    successor = coord.acquire_run_leadership(
        run_id=run_id,
        worker_id="leader-transform-successor",
        now=now + timedelta(seconds=2),
        window_seconds=WINDOW,
    )
    try:
        before_equality = capture_state_engine_image(db, run_id=run_id)
        assert repo.recover_expired_leases(now=lease_expiry, coordination_token=successor) == 0
        after_equality = capture_state_engine_image(db, run_id=run_id)
        assert before_equality.diff(after_equality).changed_columns == {"run_coordination": {"leader_heartbeat_expires_at", "updated_at"}}
        with db.read_only_connection() as conn:
            equality_seat = conn.execute(
                select(
                    run_coordination_table.c.leader_heartbeat_expires_at,
                    run_coordination_table.c.updated_at,
                ).where(run_coordination_table.c.run_id == run_id)
            ).one()
        assert tuple(equality_seat) == (lease_expiry + timedelta(seconds=WINDOW), lease_expiry)
        equality_image = _work_item_row(db, run_id=run_id)
        assert equality_image["work_item_id"] == original.work_item_id
        assert equality_image["status"] == TokenWorkStatus.LEASED.value
        assert equality_image["attempt"] == 1

        assert repo.recover_expired_leases(now=lease_expiry + timedelta(microseconds=1), coordination_token=successor) == 1
        recovered = _work_item_row(db, run_id=run_id)
        assert recovered["work_item_id"] != original.work_item_id
        assert recovered["status"] == TokenWorkStatus.READY.value
        assert recovered["attempt"] == 2
        assert recovered["lease_owner"] is None
        assert _scheduler_event_types(db, run_id=run_id).count(SchedulerEventType.RECOVER_EXPIRED_LEASE.value) == 1
    finally:
        db.close()


@pytest.mark.timeout(120)
def test_postgresql_sink_redrive_recovery_excludes_expiry_equality_and_preserves_bundle(postgres_url: str) -> None:
    """TS-06 preserves a legal COALESCED bundle through claim and recovery."""
    now = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)
    lease_expiry = now + timedelta(seconds=10)
    run_id = "run-sink-recovery-boundary"
    original_leader = "leader-sink-original"
    join_group_id = "join-postgresql-sink-recovery"
    db = LandscapeDB.from_url(postgres_url)
    original_token = _seed(
        db.engine,
        run_id=run_id,
        leader_id=original_leader,
        worker_id=None,
        worker_heartbeat_expires_at=None,
        now=now,
        leader_window_seconds=1,
    )
    repo = TokenSchedulerRepository(db.engine)
    coord = RunCoordinationRepository(db.engine)
    item = _enqueue_ready_item(
        db.engine,
        run_id=run_id,
        token_id="tok-sink-recovery",
        now=now,
        join_group_id=join_group_id,
    )
    claimed = repo.claim_ready(run_id=run_id, lease_owner=original_token.worker_id, lease_seconds=30, now=now)
    assert claimed is not None
    pending = repo.mark_pending_sink(
        work_item_id=item.work_item_id,
        row_payload_json=item.row_payload_json,
        sink_name="sink-redrive",
        outcome=TerminalOutcome.SUCCESS.value,
        path=TerminalPath.COALESCED.value,
        error_hash=None,
        error_message=None,
        now=now,
        expected_lease_owner=original_token.worker_id,
        worker_id=original_token.worker_id,
    )
    redrive = repo.claim_pending_sink(run_id=run_id, lease_owner=original_token.worker_id, lease_seconds=10, now=now)
    assert redrive is not None and redrive.lease_expires_at == lease_expiry
    bundle_columns = (
        "work_item_id",
        "attempt",
        "row_payload_json",
        "pending_sink_name",
        "pending_outcome",
        "pending_path",
        "pending_error_hash",
        "pending_error_message",
        "join_group_id",
    )
    before_bundle = tuple(_work_item_row(db, run_id=run_id)[column] for column in bundle_columns)
    assert before_bundle[0] == pending.work_item_id
    assert before_bundle[-1] == join_group_id
    assert (
        redrive.work_item_id,
        redrive.attempt,
        redrive.row_payload_json,
        redrive.pending_sink_name,
        redrive.pending_outcome,
        redrive.pending_path,
        redrive.pending_error_hash,
        redrive.pending_error_message,
        redrive.join_group_id,
    ) == before_bundle
    successor = coord.acquire_run_leadership(
        run_id=run_id,
        worker_id="leader-sink-successor",
        now=now + timedelta(seconds=2),
        window_seconds=WINDOW,
    )
    try:
        before_equality = capture_state_engine_image(db, run_id=run_id)
        assert repo.recover_expired_leases(now=lease_expiry, coordination_token=successor) == 0
        after_equality = capture_state_engine_image(db, run_id=run_id)
        assert before_equality.diff(after_equality).changed_columns == {"run_coordination": {"leader_heartbeat_expires_at", "updated_at"}}
        with db.read_only_connection() as conn:
            equality_seat = conn.execute(
                select(
                    run_coordination_table.c.leader_heartbeat_expires_at,
                    run_coordination_table.c.updated_at,
                ).where(run_coordination_table.c.run_id == run_id)
            ).one()
        assert tuple(equality_seat) == (lease_expiry + timedelta(seconds=WINDOW), lease_expiry)
        assert tuple(_work_item_row(db, run_id=run_id)[column] for column in bundle_columns) == before_bundle

        assert repo.recover_expired_leases(now=lease_expiry + timedelta(microseconds=1), coordination_token=successor) == 1
        recovered = _work_item_row(db, run_id=run_id)
        assert tuple(recovered[column] for column in bundle_columns) == before_bundle
        assert recovered["status"] == TokenWorkStatus.PENDING_SINK.value
        assert recovered["lease_owner"] is None
        assert recovered["lease_expires_at"] is None
        assert _scheduler_event_types(db, run_id=run_id).count(SchedulerEventType.RECOVER_EXPIRED_LEASE.value) == 1
    finally:
        db.close()


@pytest.mark.timeout(120)
def test_postgresql_eviction_exact_boundary_is_inert_until_strictly_expired(postgres_url: str) -> None:
    """RC-07 backend semantics only; this does not enable PostgreSQL followers."""
    now = datetime(2026, 8, 12, 5, 0, tzinfo=UTC)
    run_id = "run-eviction-boundary"
    worker_id = "worker-eviction-boundary"
    db = LandscapeDB.from_url(postgres_url)
    token = _seed(
        db.engine,
        run_id=run_id,
        leader_id="leader-eviction-boundary",
        worker_id=worker_id,
        worker_heartbeat_expires_at=now - timedelta(seconds=GRACE),
        now=now,
    )
    coord = RunCoordinationRepository(db.engine)
    try:
        before_equality = capture_state_engine_image(db, run_id=run_id)
        assert (
            coord.evict_worker(
                token=token,
                target_worker_id=worker_id,
                now=now,
                grace_seconds=GRACE,
                window_seconds=WINDOW,
            )
            is False
        )
        assert capture_state_engine_image(db, run_id=run_id) == before_equality
        assert _worker_status_and_live_leases(db, run_id=run_id, worker_id=worker_id, now=now) == ("active", [])

        after_boundary = now + timedelta(microseconds=1)
        assert (
            coord.evict_worker(
                token=token,
                target_worker_id=worker_id,
                now=after_boundary,
                grace_seconds=GRACE,
                window_seconds=WINDOW,
            )
            is True
        )
        assert _worker_status_and_live_leases(db, run_id=run_id, worker_id=worker_id, now=after_boundary) == ("evicted", [])
    finally:
        db.close()


def _backend_pid(conn: Any) -> int:
    return int(conn.connection.driver_connection.info.backend_pid)


def _await_done_or_lock_wait(
    db: LandscapeDB,
    *,
    done: threading.Event,
    pid_holder: dict[str, int],
    pid_key: str,
    timeout: float = 30.0,
) -> str:
    """Wait until the observed thread finished OR its backend blocks on a row lock.

    Returns ``"done"`` or ``"lock_wait"``.  Bounded condition poll on
    ``pg_stat_activity`` — the loop exits on an observed state, never on time.
    """
    deadline = time.monotonic() + timeout
    with db.engine.connect() as monitor:
        while time.monotonic() < deadline:
            if done.is_set():
                return "done"
            pid = pid_holder.get(pid_key)
            if pid is not None:
                wait_event_type = monitor.exec_driver_sql(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %(pid)s",
                    {"pid": pid},
                ).scalar()
                if wait_event_type == "Lock":
                    return "lock_wait"
            time.sleep(0.02)
    raise AssertionError(f"thread neither finished nor blocked on a lock within {timeout}s")


def _worker_status_and_live_leases(db: LandscapeDB, *, run_id: str, worker_id: str, now: datetime) -> tuple[str, list[str]]:
    with db.read_only_connection() as conn:
        status = conn.execute(
            select(run_workers_table.c.status).where(
                run_workers_table.c.worker_id == worker_id,
                run_workers_table.c.run_id == run_id,
            )
        ).scalar_one()
        leases = (
            conn.execute(
                select(token_work_items_table.c.work_item_id, token_work_items_table.c.lease_expires_at)
                .where(token_work_items_table.c.run_id == run_id)
                .where(token_work_items_table.c.status == "leased")
                .where(token_work_items_table.c.lease_owner == worker_id)
            )
            .mappings()
            .all()
        )
    live = [str(lease["work_item_id"]) for lease in leases if lease["lease_expires_at"] and lease["lease_expires_at"] > now]
    return str(status), live


def _pause_evictor_after_registry_update(
    pids: dict[str, int],
    paused: threading.Event,
    release: threading.Event,
) -> Callable[..., None]:
    def hook(conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if threading.current_thread().name != "evictor":
            return
        pids.setdefault("evictor", _backend_pid(conn))
        if " ".join(statement.upper().split()).startswith("UPDATE RUN_WORKERS"):
            paused.set()
            if not release.wait(timeout=30):
                raise TimeoutError("test never released the eviction transaction")

    return hook


def _record_thread_pid(pids: dict[str, int], thread_name: str, key: str) -> Callable[..., None]:
    def hook(conn, _cursor, _statement, _parameters, _context, _executemany) -> None:
        if threading.current_thread().name == thread_name:
            pids.setdefault(key, _backend_pid(conn))

    return hook


@pytest.mark.timeout(120)
def test_fenced_claim_blocks_behind_in_flight_eviction_and_is_refused(postgres_url: str) -> None:
    """A claim racing an uncommitted eviction must observe it and be refused."""
    now = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
    run_id = "run-claim-vs-evict"
    worker_id = "worker-claim"
    db = LandscapeDB.from_url(postgres_url)
    token = _seed(
        db.engine,
        run_id=run_id,
        leader_id="leader-claim",
        worker_id=worker_id,
        worker_heartbeat_expires_at=now - timedelta(seconds=GRACE + 10),
        now=now,
    )
    _enqueue_ready_item(db.engine, run_id=run_id, token_id="tok-claim", now=now)
    scheduler = TokenSchedulerRepository(db.engine)
    coord = RunCoordinationRepository(db.engine)

    pids: dict[str, int] = {}
    evictor_paused = threading.Event()
    release_evictor = threading.Event()
    claimant_done = threading.Event()
    results: dict[str, object] = {}

    pause_hook = _pause_evictor_after_registry_update(pids, evictor_paused, release_evictor)
    pid_hook = _record_thread_pid(pids, "claimant", "claimant")
    event.listen(db.engine, "after_cursor_execute", pause_hook)
    event.listen(db.engine, "before_cursor_execute", pid_hook)

    def evict() -> None:
        try:
            results["evict"] = coord.evict_worker(
                token=token,
                target_worker_id=worker_id,
                now=now,
                grace_seconds=GRACE,
                window_seconds=WINDOW,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            results["evict"] = f"RAISED {type(exc).__name__}: {exc}"

    def claim() -> None:
        try:
            results["claim"] = scheduler.claim_ready(run_id=run_id, lease_owner=worker_id, lease_seconds=300, now=now)
        except BaseException as exc:
            results["claim"] = exc
        finally:
            claimant_done.set()

    evictor = threading.Thread(target=evict, name="evictor")
    claimant = threading.Thread(target=claim, name="claimant")
    try:
        evictor.start()
        assert evictor_paused.wait(timeout=30), "eviction never reached its registry UPDATE"
        claimant.start()
        state = _await_done_or_lock_wait(db, done=claimant_done, pid_holder=pids, pid_key="claimant")
        assert state == "lock_wait", (
            "the fenced claim must BLOCK behind the in-flight eviction's "
            f"registry row lock, but it {state} with result {results.get('claim')!r} "
            "— the membership fence did not serialize with eviction "
            "(elspeth-6903f82511)"
        )
        release_evictor.set()
        evictor.join(timeout=60)
        claimant.join(timeout=60)
        assert not evictor.is_alive() and not claimant.is_alive(), "race threads wedged"
    finally:
        release_evictor.set()
        if evictor.ident is not None:
            evictor.join(timeout=30)
        if claimant.ident is not None:
            claimant.join(timeout=30)
        event.remove(db.engine, "before_cursor_execute", pid_hook)
        event.remove(db.engine, "after_cursor_execute", pause_hook)

    try:
        assert results["evict"] is True, f"eviction must commit, got: {results['evict']!r}"
        assert isinstance(results["claim"], RunWorkerEvictedError), (
            f"the racing claim must be refused with RunWorkerEvictedError, got: {results['claim']!r}"
        )
        status, live = _worker_status_and_live_leases(db, run_id=run_id, worker_id=worker_id, now=now)
        assert status == "evicted"
        assert live == [], f"an evicted worker must not hold a live lease, found: {live}"
    finally:
        db.close()


@pytest.mark.timeout(120)
def test_heartbeat_renewal_blocks_behind_in_flight_eviction_and_is_refused(postgres_url: str) -> None:
    """A renewal racing an uncommitted eviction must observe it and be refused."""
    t0 = datetime(2026, 7, 16, 11, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=GRACE + 100)
    run_id = "run-heartbeat-vs-evict"
    worker_id = "worker-heartbeat"
    db = LandscapeDB.from_url(postgres_url)
    token = _seed(
        db.engine,
        run_id=run_id,
        leader_id="leader-heartbeat",
        worker_id=worker_id,
        worker_heartbeat_expires_at=t0,  # stale by t1
        now=t0,
    )
    _enqueue_ready_item(db.engine, run_id=run_id, token_id="tok-heartbeat", now=t0)
    scheduler = TokenSchedulerRepository(db.engine)
    coord = RunCoordinationRepository(db.engine)
    item = scheduler.claim_ready(run_id=run_id, lease_owner=worker_id, lease_seconds=10, now=t0)
    assert item is not None  # lease is expired well before t1

    pids: dict[str, int] = {}
    evictor_paused = threading.Event()
    release_evictor = threading.Event()
    heartbeat_done = threading.Event()
    results: dict[str, object] = {}

    pause_hook = _pause_evictor_after_registry_update(pids, evictor_paused, release_evictor)
    pid_hook = _record_thread_pid(pids, "heartbeater", "heartbeater")
    event.listen(db.engine, "after_cursor_execute", pause_hook)
    event.listen(db.engine, "before_cursor_execute", pid_hook)

    def evict() -> None:
        try:
            results["evict"] = coord.evict_worker(
                token=token,
                target_worker_id=worker_id,
                now=t1,
                grace_seconds=GRACE,
                window_seconds=WINDOW,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            results["evict"] = f"RAISED {type(exc).__name__}: {exc}"

    def heartbeat() -> None:
        try:
            results["heartbeat"] = scheduler.heartbeat_lease(
                run_id=run_id,
                work_item_id=item.work_item_id,
                lease_owner=worker_id,
                lease_seconds=300,
                now=t1,
                membership_fenced=True,
            )
        except BaseException as exc:
            results["heartbeat"] = exc
        finally:
            heartbeat_done.set()

    evictor = threading.Thread(target=evict, name="evictor")
    heartbeater = threading.Thread(target=heartbeat, name="heartbeater")
    try:
        evictor.start()
        assert evictor_paused.wait(timeout=30), "eviction never reached its registry UPDATE"
        heartbeater.start()
        state = _await_done_or_lock_wait(db, done=heartbeat_done, pid_holder=pids, pid_key="heartbeater")
        assert state == "lock_wait", (
            "the fenced renewal must BLOCK behind the in-flight eviction's "
            f"registry row lock, but it {state} with result {results.get('heartbeat')!r} "
            "— the membership fence did not serialize with eviction "
            "(elspeth-6903f82511)"
        )
        release_evictor.set()
        evictor.join(timeout=60)
        heartbeater.join(timeout=60)
        assert not evictor.is_alive() and not heartbeater.is_alive(), "race threads wedged"
    finally:
        release_evictor.set()
        if evictor.ident is not None:
            evictor.join(timeout=30)
        if heartbeater.ident is not None:
            heartbeater.join(timeout=30)
        event.remove(db.engine, "before_cursor_execute", pid_hook)
        event.remove(db.engine, "after_cursor_execute", pause_hook)

    try:
        assert results["evict"] is True, f"eviction must commit, got: {results['evict']!r}"
        assert isinstance(results["heartbeat"], RunWorkerEvictedError), (
            f"the racing renewal must be refused with RunWorkerEvictedError, got: {results['heartbeat']!r}"
        )
        status, live = _worker_status_and_live_leases(db, run_id=run_id, worker_id=worker_id, now=t1)
        assert status == "evicted"
        assert live == [], f"an evicted worker must not retain a renewed lease, found: {live}"
    finally:
        db.close()


@pytest.mark.timeout(120)
def test_eviction_defers_to_in_flight_fenced_claim(postgres_url: str) -> None:
    """Eviction racing an uncommitted fenced claim must block, then skip.

    The mirror interleaving: the claimant holds its shared membership lock
    with the lease CAS uncommitted.  ``evict_worker`` must block on the
    registry row BEFORE its no-unexpired-leases precondition, then observe the
    committed lease and return False — never evict a worker that just
    acquired a live lease.
    """
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)
    run_id = "run-evict-vs-claim"
    worker_id = "worker-defer"
    db = LandscapeDB.from_url(postgres_url)
    token = _seed(
        db.engine,
        run_id=run_id,
        leader_id="leader-defer",
        worker_id=worker_id,
        worker_heartbeat_expires_at=now - timedelta(seconds=GRACE + 10),  # evictable heartbeat
        now=now,
    )
    _enqueue_ready_item(db.engine, run_id=run_id, token_id="tok-defer", now=now)
    scheduler = TokenSchedulerRepository(db.engine)
    coord = RunCoordinationRepository(db.engine)

    pids: dict[str, int] = {}
    claimant_paused = threading.Event()
    release_claimant = threading.Event()
    evictor_done = threading.Event()
    results: dict[str, object] = {}

    def pause_claimant_after_cas(conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if threading.current_thread().name != "claimant":
            return
        if " ".join(statement.upper().split()).startswith("UPDATE TOKEN_WORK_ITEMS"):
            claimant_paused.set()
            if not release_claimant.wait(timeout=30):
                raise TimeoutError("test never released the claim transaction")

    pid_hook = _record_thread_pid(pids, "evictor", "evictor")
    event.listen(db.engine, "after_cursor_execute", pause_claimant_after_cas)
    event.listen(db.engine, "before_cursor_execute", pid_hook)

    def claim() -> None:
        try:
            results["claim"] = scheduler.claim_ready(run_id=run_id, lease_owner=worker_id, lease_seconds=300, now=now)
        except BaseException as exc:  # pragma: no cover - asserted below
            results["claim"] = f"RAISED {type(exc).__name__}: {exc}"

    def evict() -> None:
        try:
            results["evict"] = coord.evict_worker(
                token=token,
                target_worker_id=worker_id,
                now=now,
                grace_seconds=GRACE,
                window_seconds=WINDOW,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            results["evict"] = f"RAISED {type(exc).__name__}: {exc}"
        finally:
            evictor_done.set()

    claimant = threading.Thread(target=claim, name="claimant")
    evictor = threading.Thread(target=evict, name="evictor")
    try:
        claimant.start()
        assert claimant_paused.wait(timeout=30), "claim never reached its CAS UPDATE"
        evictor.start()
        state = _await_done_or_lock_wait(db, done=evictor_done, pid_holder=pids, pid_key="evictor")
        assert state == "lock_wait", (
            "eviction must BLOCK behind the in-flight fenced claim's shared "
            f"membership lock, but it {state} with result {results.get('evict')!r} "
            "— evict_worker read its live-lease precondition without "
            "serializing with the fence (elspeth-6903f82511)"
        )
        release_claimant.set()
        claimant.join(timeout=60)
        evictor.join(timeout=60)
        assert not claimant.is_alive() and not evictor.is_alive(), "race threads wedged"
    finally:
        release_claimant.set()
        if claimant.ident is not None:
            claimant.join(timeout=30)
        if evictor.ident is not None:
            evictor.join(timeout=30)
        event.remove(db.engine, "before_cursor_execute", pid_hook)
        event.remove(db.engine, "after_cursor_execute", pause_claimant_after_cas)

    try:
        assert results["evict"] is False, f"eviction must defer to the committed live lease and skip, got: {results['evict']!r}"
        assert results["claim"] is not None and not isinstance(results["claim"], str), (
            f"the fenced claim must succeed, got: {results['claim']!r}"
        )
        status, live = _worker_status_and_live_leases(db, run_id=run_id, worker_id=worker_id, now=now)
        assert status == "active", "a worker holding a live lease must not be evicted"
        assert len(live) == 1
    finally:
        db.close()
