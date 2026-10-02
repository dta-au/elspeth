"""Scheduler deadlines are decided after PostgreSQL grants the item lock."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Connection, ExecutionContext
from tests.helpers.postgres_target import postgres_test_target
from tests.testcontainer.core.test_scheduler_lease_eviction_postgres import _enqueue_ready_item, _seed

from elspeth.contracts.coordination import WorkerMembershipToken
from elspeth.contracts.errors import SchedulerLeaseLostError
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import run_workers_table, scheduler_events_table, token_work_items_table

pytestmark = [pytest.mark.testcontainer, pytest.mark.timeout(60)]


@pytest.fixture
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


def _wait_behind_item(
    db: LandscapeDB,
    item: TokenWorkItem,
    operation: Callable[[], object],
    *,
    replace_owner: bool = False,
) -> tuple[object, datetime]:
    """Observe the exact blocker, then charge a known delay to lock waiting."""
    pids: list[int] = []
    results: list[object] = []
    done = threading.Event()

    def capture_pid(conn: Connection) -> None:
        if threading.current_thread().name == "scheduler-clock-contender":
            pids.append(conn.execute(text("SELECT pg_backend_pid()")).scalar_one())
            conn.execute(text("SET LOCAL statement_timeout = '15s'"))

    def invoke() -> None:
        try:
            results.append(operation())
        except BaseException as exc:
            results.append(exc)
        finally:
            done.set()

    contender = threading.Thread(target=invoke, name="scheduler-clock-contender")
    event.listen(db.engine, "begin", capture_pid)
    try:
        with db.engine.begin() as holder:
            holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()
            holder.execute(
                select(token_work_items_table.c.work_item_id)
                .where(token_work_items_table.c.work_item_id == item.work_item_id)
                .with_for_update()
            ).one()
            contender.start()
            deadline = time.monotonic() + 10
            with db.engine.connect() as observer:
                while True:
                    if pids:
                        blockers = observer.execute(text("SELECT pg_blocking_pids(:pid)"), {"pid": pids[0]}).scalar_one()
                        if holder_pid in blockers:
                            break
                    assert not done.is_set(), f"operation did not wait for its item lock: {results!r}"
                    assert time.monotonic() < deadline, "PostgreSQL never reported the held item as blocker"
                    time.sleep(0.01)
            holder.execute(text("SELECT pg_sleep(1.2)"))
            if replace_owner:
                holder.execute(
                    update(token_work_items_table)
                    .where(token_work_items_table.c.work_item_id == item.work_item_id)
                    .values(lease_owner="replacement")
                )
            released_at = holder.execute(text("SELECT clock_timestamp()")).scalar_one()
        contender.join(timeout=20)
        assert not contender.is_alive(), "scheduler operation remained blocked after release"
        assert len(results) == 1
        return results[0], released_at
    finally:
        if contender.ident is not None:
            contender.join(timeout=20)
        event.remove(db.engine, "begin", capture_pid)


@pytest.mark.parametrize("verb", ["ready", "pending_sink", "heartbeat"])
def test_issuance_uses_fresh_time_after_actual_item_lock(postgres_url: str, verb: str) -> None:
    with LandscapeDB.from_url(postgres_url) as db:
        now = datetime.now(UTC)
        run_id = f"fresh-{verb}"
        authority = _seed(
            db.engine,
            run_id=run_id,
            leader_id="leader",
            worker_id="worker",
            worker_heartbeat_expires_at=now + timedelta(minutes=5),
            now=now,
        )
        item = _enqueue_ready_item(db.engine, run_id=run_id, token_id=f"token-{verb}", now=now)
        repo = TokenSchedulerRepository(db.engine)
        member = WorkerMembershipToken(run_id=run_id, worker_id="worker")
        if verb == "pending_sink":
            with db.engine.begin() as conn:
                conn.execute(
                    update(token_work_items_table)
                    .where(token_work_items_table.c.work_item_id == item.work_item_id)
                    .values(
                        status="pending_sink",
                        pending_sink_name="output",
                        pending_outcome="success",
                        pending_path="default_flow",
                    )
                )
            operation = partial(repo.claim_pending_sink, coordination_token=authority, lease_owner="leader", lease_seconds=2)
        elif verb == "heartbeat":
            claimed = repo.claim_ready(member_token=member, lease_owner="worker", lease_seconds=1)
            assert claimed is not None
            operation = partial(
                repo.heartbeat_lease,
                member_token=member,
                work_item_id=item.work_item_id,
                lease_owner="worker",
                lease_seconds=2,
            )
        else:
            operation = partial(repo.claim_ready, member_token=member, lease_owner="worker", lease_seconds=2)
        result, released_at = _wait_behind_item(db, item, operation)
        assert not isinstance(result, BaseException), repr(result)
        with db.engine.connect() as conn:
            persisted = (
                conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == item.work_item_id))
                .mappings()
                .one()
            )
            expiry = persisted["lease_expires_at"]
            assert expiry >= released_at + timedelta(seconds=1.9), "item wait consumed the newly issued lease"
            assert expiry == persisted["updated_at"] + timedelta(seconds=2)
            if verb == "heartbeat":
                assert claimed.lease_expires_at is not None and claimed.lease_expires_at < released_at
                assert result == expiry
            else:
                assert isinstance(result, TokenWorkItem)
                assert result.lease_expires_at == expiry
                event_expiry = conn.execute(
                    select(scheduler_events_table.c.to_lease_expires_at).where(
                        scheduler_events_table.c.work_item_id == item.work_item_id,
                        scheduler_events_table.c.event_type == f"claim_{verb}",
                    )
                ).scalar_one()
                assert event_expiry == expiry


def test_waiting_heartbeat_refuses_changed_item_owner(postgres_url: str) -> None:
    with LandscapeDB.from_url(postgres_url) as db:
        now = datetime.now(UTC)
        run_id = "fresh-heartbeat-stale-owner"
        _seed(
            db.engine,
            run_id=run_id,
            leader_id="leader",
            worker_id="worker",
            worker_heartbeat_expires_at=now + timedelta(minutes=5),
            now=now,
        )
        item = _enqueue_ready_item(db.engine, run_id=run_id, token_id="token-stale", now=now)
        member = WorkerMembershipToken(run_id=run_id, worker_id="worker")
        repo = TokenSchedulerRepository(db.engine)
        claimed = repo.claim_ready(member_token=member, lease_owner="worker", lease_seconds=60)
        assert claimed is not None
        result, _ = _wait_behind_item(
            db,
            item,
            lambda: repo.heartbeat_lease(
                member_token=member,
                work_item_id=item.work_item_id,
                lease_owner="worker",
                lease_seconds=2,
            ),
            replace_owner=True,
        )
        assert isinstance(result, SchedulerLeaseLostError)
        with db.engine.connect() as conn:
            row = (
                conn.execute(select(token_work_items_table).where(token_work_items_table.c.work_item_id == item.work_item_id))
                .mappings()
                .one()
            )
        assert row["lease_owner"] == "replacement"
        assert row["lease_expires_at"] == claimed.lease_expires_at


def test_recovery_rechecks_owner_expiration_after_item_lock_wait(postgres_url: str) -> None:
    with LandscapeDB.from_url(postgres_url) as db:
        now = datetime.now(UTC)
        run_id = "fresh-recovery-expiry-crossing"
        authority = _seed(
            db.engine,
            run_id=run_id,
            leader_id="leader",
            worker_id="worker",
            worker_heartbeat_expires_at=now + timedelta(minutes=5),
            now=now,
        )
        item = _enqueue_ready_item(db.engine, run_id=run_id, token_id="token-crossing", now=now)
        member = WorkerMembershipToken(run_id=run_id, worker_id="worker")
        repo = TokenSchedulerRepository(db.engine)
        claimed = repo.claim_ready(member_token=member, lease_owner="worker", lease_seconds=60)
        assert claimed is not None
        with db.engine.begin() as conn:
            stamp = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
            conn.execute(
                update(token_work_items_table)
                .where(token_work_items_table.c.work_item_id == item.work_item_id)
                .values(
                    lease_expires_at=stamp - timedelta(seconds=1),
                )
            )
        discovery: list[tuple[datetime, datetime]] = []

        def expire_owner_during_discovered_item_wait(
            conn: Connection, cursor: object, statement: str, parameters: object, context: ExecutionContext, executemany: bool
        ) -> None:
            if discovery or threading.current_thread().name != "scheduler-clock-contender":
                return
            normalized = " ".join(statement.upper().split())
            if not normalized.startswith("SELECT TOKEN_WORK_ITEMS.WORK_ITEM_ID, TOKEN_WORK_ITEMS.LEASE_OWNER"):
                return
            # The discovery sample has already been read. Set the crossing
            # relative to this witnessed boundary, not earlier test setup.
            observed_at = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
            owner_expiry = observed_at + timedelta(seconds=0.6)
            discovery.append((observed_at, owner_expiry))
            conn.execute(
                update(run_workers_table)
                .where(run_workers_table.c.worker_id == "worker")
                .values(
                    heartbeat_expires_at=owner_expiry,
                )
            )

        event.listen(db.engine, "before_cursor_execute", expire_owner_during_discovered_item_wait)
        try:
            result, released_at = _wait_behind_item(
                db,
                item,
                lambda: repo.recover_expired_leases(
                    coordination_token=authority,
                    grace_seconds=0,
                    stall_budget_seconds=10000,
                ),
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", expire_owner_during_discovered_item_wait)
        assert len(discovery) == 1
        observed_at, owner_expiry = discovery[0]
        assert observed_at < owner_expiry <= released_at
        assert result == 1
        with db.engine.connect() as conn:
            recovered = conn.execute(select(token_work_items_table).where(token_work_items_table.c.run_id == run_id)).mappings().one()
            recovery_event = (
                conn.execute(
                    select(scheduler_events_table).where(
                        scheduler_events_table.c.run_id == run_id,
                        scheduler_events_table.c.event_type == "recover_expired_lease",
                    )
                )
                .mappings()
                .one()
            )
        assert recovered["status"] == "ready"
        assert recovered["attempt"] == claimed.attempt + 1
        assert recovered["work_item_id"] != claimed.work_item_id
        assert recovery_event["recorded_at"] >= released_at
