"""PostgreSQL proof for release-seat/takeover lock ordering (RC-04)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, insert, select
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
from tests.helpers.state_engine import capture_state_engine_image

from elspeth.contracts.coordination import CoordinationToken, mint_worker_id
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.schema import run_coordination_events_table, run_coordination_table, run_workers_table, runs_table

pytestmark = pytest.mark.testcontainer

NOW = datetime(2026, 7, 23, tzinfo=UTC)
RUN_ID = "release-seat-vs-takeover"


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()


def _seed_run(db: LandscapeDB, *, run_id: str, now: datetime, status: str = "running") -> None:
    with db.engine.begin() as conn:
        conn.execute(
            insert(runs_table).values(
                run_id=run_id,
                started_at=now,
                config_hash="config",
                settings_json="{}",
                canonical_version="v1",
                status=status,
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        )


def _coordination_events(db: LandscapeDB, *, run_id: str) -> list[dict[str, object]]:
    with db.read_only_connection() as conn:
        rows = (
            conn.execute(
                select(run_coordination_events_table)
                .where(run_coordination_events_table.c.run_id == run_id)
                .order_by(run_coordination_events_table.c.seq)
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _set_postgresql_transaction_timeouts(conn: Any) -> None:
    conn.exec_driver_sql("SET LOCAL statement_timeout = '15000ms'")
    conn.exec_driver_sql("SET LOCAL lock_timeout = '5000ms'")


def _run_takeover_contenders(
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

    def pause_before_seat_update(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _params: Any,
        _context: Any,
        _many: bool,
    ) -> None:
        name = threading.current_thread().name
        contender = {"first-takeover": "first", "second-takeover": "second"}.get(name)
        if contender is None or reached_update[contender].is_set():
            return
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("UPDATE RUN_COORDINATION"):
            reached_update[contender].set()
            if not release_update.wait(timeout=15):
                raise TimeoutError(f"{contender} takeover timed out at the pre-UPDATE race seam")

    def invoke(name: str, operation: Callable[[], object]) -> None:
        try:
            outcomes[name] = operation()
        except BaseException as exc:  # pragma: no cover - asserted by caller
            outcomes[name] = exc

    threads = (
        threading.Thread(target=invoke, args=("first", first), name="first-takeover"),
        threading.Thread(target=invoke, args=("second", second), name="second-takeover"),
    )
    engines = (first_db.engine, second_db.engine)
    for engine in engines:
        event.listen(engine, "begin", _set_postgresql_transaction_timeouts)
        event.listen(engine, "before_cursor_execute", pause_before_seat_update)
    started: list[threading.Thread] = []
    teardown_failure: str | None = None
    try:
        for thread in threads:
            thread.start()
            started.append(thread)

        deadline = time.monotonic() + 15
        while not all(gate.is_set() for gate in reached_update.values()):
            exited_early = [name for name, gate in reached_update.items() if name in outcomes and not gate.is_set()]
            assert not exited_early, f"takeover contenders exited before the pre-UPDATE race seam: {exited_early!r}"
            if time.monotonic() >= deadline:
                missing = [name for name, gate in reached_update.items() if not gate.is_set()]
                raise AssertionError(f"takeover contenders did not reach the pre-UPDATE race seam: {missing!r}")
            time.sleep(0.01)

        release_update.set()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads), "takeover contenders did not finish within the bounded wait"
    finally:
        release_update.set()
        for thread in started:
            thread.join(timeout=20)
        alive = [thread.name for thread in started if thread.is_alive()]
        if alive:
            teardown_failure = f"takeover contender teardown remained live after bounded joins: {alive!r}"
        for engine in engines:
            event.remove(engine, "before_cursor_execute", pause_before_seat_update)
            event.remove(engine, "begin", _set_postgresql_transaction_timeouts)
    assert teardown_failure is None, teardown_failure
    return outcomes["first"], outcomes["second"]


@pytest.mark.timeout(120)
def test_postgresql_initial_leader_registration_is_atomic(postgres_url: str) -> None:
    """RC-01 seat, membership, and evidence roll back and commit together."""
    now = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)
    run_id = "run-postgresql-register-atomic"
    worker_id = mint_worker_id(run_id)
    db = LandscapeDB.from_url(postgres_url)
    repo = RunCoordinationRepository(db.engine)
    _seed_run(db, run_id=run_id, now=now)
    before = capture_state_engine_image(db, run_id=run_id)

    def fail_first_evidence(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _params: Any,
        _context: Any,
        _many: bool,
    ) -> None:
        if " ".join(statement.upper().split()).startswith("INSERT INTO RUN_COORDINATION_EVENTS"):
            raise RuntimeError("forced PostgreSQL coordination evidence failure")

    event.listen(db.engine, "before_cursor_execute", fail_first_evidence)
    try:
        with pytest.raises(RuntimeError, match="forced PostgreSQL coordination evidence failure"):
            repo.register_run_leader(run_id=run_id, worker_id=worker_id, now=now, window_seconds=30)
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_first_evidence)

    assert capture_state_engine_image(db, run_id=run_id) == before
    token = repo.register_run_leader(run_id=run_id, worker_id=worker_id, now=now, window_seconds=30)
    try:
        assert token == CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=1)
        with db.read_only_connection() as conn:
            seat = conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == run_id)).mappings().one()
            member = conn.execute(select(run_workers_table).where(run_workers_table.c.worker_id == worker_id)).mappings().one()
        assert (seat["leader_worker_id"], seat["leader_epoch"], seat["leader_heartbeat_expires_at"]) == (
            worker_id,
            1,
            now + timedelta(seconds=30),
        )
        assert (member["run_id"], member["role"], member["status"], member["heartbeat_expires_at"]) == (
            run_id,
            "leader",
            "active",
            now + timedelta(seconds=30),
        )
        events = _coordination_events(db, run_id=run_id)
        assert [row["event_type"] for row in events] == ["worker_register", "leader_acquire"]
        assert all(row["leader_epoch"] == 1 for row in events)
    finally:
        db.close()


@pytest.mark.timeout(120)
def test_postgresql_takeover_excludes_exact_expiry_then_admits_after_boundary(postgres_url: str) -> None:
    """RC-02 uses a strict-expiry conditional update at microsecond precision."""
    now = datetime(2026, 8, 12, 7, 0, tzinfo=UTC)
    expiry = now + timedelta(seconds=1)
    run_id = "run-postgresql-takeover-boundary"
    incumbent_id = mint_worker_id(run_id)
    db = LandscapeDB.from_url(postgres_url)
    repo = RunCoordinationRepository(db.engine)
    _seed_run(db, run_id=run_id, now=now, status="failed")
    repo.register_run_leader(run_id=run_id, worker_id=incumbent_id, now=now, window_seconds=1)
    before_equality = capture_state_engine_image(db, run_id=run_id)
    equality_contender = mint_worker_id(run_id)

    with pytest.raises(NonResumableRunError, match="run leadership is held by"):
        repo.acquire_run_leadership(
            run_id=run_id,
            worker_id=equality_contender,
            now=expiry,
            window_seconds=30,
        )
    assert capture_state_engine_image(db, run_id=run_id) == before_equality

    successor_id = mint_worker_id(run_id)
    token = repo.acquire_run_leadership(
        run_id=run_id,
        worker_id=successor_id,
        now=expiry + timedelta(microseconds=1),
        window_seconds=30,
    )
    try:
        assert token == CoordinationToken(run_id=run_id, worker_id=successor_id, leader_epoch=2)
        with db.read_only_connection() as conn:
            seat = conn.execute(
                select(run_coordination_table.c.leader_worker_id, run_coordination_table.c.leader_epoch).where(
                    run_coordination_table.c.run_id == run_id
                )
            ).one()
            workers: dict[str, str] = {
                str(row["worker_id"]): str(row["status"])
                for row in conn.execute(
                    select(run_workers_table.c.worker_id, run_workers_table.c.status).where(run_workers_table.c.run_id == run_id)
                ).mappings()
            }
        assert tuple(seat) == (successor_id, 2)
        assert workers == {incumbent_id: "evicted", successor_id: "active"}
        assert equality_contender not in workers
    finally:
        db.close()


@pytest.mark.timeout(120)
def test_postgresql_concurrent_takeover_conditional_update_has_one_winner(postgres_url: str) -> None:
    """Independent connections serialize on one expired seat without double epoch."""
    now = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
    run_id = "run-postgresql-concurrent-takeover"
    incumbent_id = mint_worker_id(run_id)
    first_id = mint_worker_id(run_id)
    second_id = mint_worker_id(run_id)
    with ExitStack() as resources:
        first_db = LandscapeDB.from_url(postgres_url)
        resources.callback(first_db.close)
        second_db = LandscapeDB.from_url(postgres_url)
        resources.callback(second_db.close)
        _seed_run(first_db, run_id=run_id, now=now, status="failed")
        RunCoordinationRepository(first_db.engine).register_run_leader(
            run_id=run_id,
            worker_id=incumbent_id,
            now=now,
            window_seconds=1,
        )
        outcomes = _run_takeover_contenders(
            first_db,
            lambda: RunCoordinationRepository(first_db.engine).acquire_run_leadership(
                run_id=run_id,
                worker_id=first_id,
                now=now + timedelta(seconds=2),
                window_seconds=30,
            ),
            second_db,
            lambda: RunCoordinationRepository(second_db.engine).acquire_run_leadership(
                run_id=run_id,
                worker_id=second_id,
                now=now + timedelta(seconds=2),
                window_seconds=30,
            ),
        )
        winners = [outcome for outcome in outcomes if isinstance(outcome, CoordinationToken)]
        losers = [outcome for outcome in outcomes if isinstance(outcome, NonResumableRunError)]
        assert len(winners) == 1
        assert len(losers) == 1
        winner = winners[0]
        with first_db.read_only_connection() as conn:
            seat = conn.execute(
                select(run_coordination_table.c.leader_worker_id, run_coordination_table.c.leader_epoch).where(
                    run_coordination_table.c.run_id == run_id
                )
            ).one()
            workers: dict[str, str] = {
                str(row["worker_id"]): str(row["status"])
                for row in conn.execute(
                    select(run_workers_table.c.worker_id, run_workers_table.c.status).where(run_workers_table.c.run_id == run_id)
                ).mappings()
            }
        assert tuple(seat) == (winner.worker_id, 2)
        assert workers == {incumbent_id: "evicted", winner.worker_id: "active"}
        assert len(_coordination_events(first_db, run_id=run_id)) == 5


@pytest.mark.timeout(120)
def test_postgresql_active_leader_heartbeat_extends_both_rows_without_event(postgres_url: str) -> None:
    """RC-03 heartbeat keeps one active PostgreSQL leader live at equality."""
    now = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
    run_id = "run-postgresql-active-heartbeat"
    worker_id = mint_worker_id(run_id)
    db = LandscapeDB.from_url(postgres_url)
    repo = RunCoordinationRepository(db.engine)
    _seed_run(db, run_id=run_id, now=now, status="failed")
    repo.register_run_leader(run_id=run_id, worker_id=worker_id, now=now, window_seconds=5)
    events_before = _coordination_events(db, run_id=run_id)

    heartbeat_at = now + timedelta(seconds=5)
    snapshot = repo.worker_heartbeat(worker_id=worker_id, now=heartbeat_at, window_seconds=5)
    extended_expiry = now + timedelta(seconds=10)
    try:
        assert snapshot.worker_active is True
        assert snapshot.leader_worker_id == worker_id
        assert snapshot.leader_epoch == 1
        assert snapshot.seat_live is True
        with db.read_only_connection() as conn:
            seat_expiry = conn.execute(
                select(run_coordination_table.c.leader_heartbeat_expires_at).where(run_coordination_table.c.run_id == run_id)
            ).scalar_one()
            worker_expiry = conn.execute(
                select(run_workers_table.c.heartbeat_expires_at).where(run_workers_table.c.worker_id == worker_id)
            ).scalar_one()
        assert seat_expiry == extended_expiry
        assert worker_expiry == extended_expiry
        assert _coordination_events(db, run_id=run_id) == events_before

        with pytest.raises(NonResumableRunError, match="run leadership is held by"):
            repo.acquire_run_leadership(
                run_id=run_id,
                worker_id=mint_worker_id(run_id),
                now=extended_expiry,
                window_seconds=30,
            )
        assert _coordination_events(db, run_id=run_id) == events_before
    finally:
        db.close()


@pytest.mark.timeout(120)
def test_postgresql_departed_follower_heartbeat_cannot_revive_membership(postgres_url: str) -> None:
    """RC-06: departure is terminal for the worker identity on PostgreSQL."""
    now = datetime(2026, 8, 12, 9, 30, tzinfo=UTC)
    departed_at = now + timedelta(seconds=10)
    run_id = "run-postgresql-follower-departure"
    leader_id = "leader-follower-departure"
    follower_id = "follower-departure"
    db = LandscapeDB.from_url(postgres_url)
    try:
        repo = RunCoordinationRepository(db.engine)
        _seed_run(db, run_id=run_id, now=now)
        repo.register_run_leader(run_id=run_id, worker_id=leader_id, now=now, window_seconds=30)
        repo.admit_follower(
            run_id=run_id,
            worker_id=follower_id,
            config_hash="config",
            now=now,
            window_seconds=30,
        )
        with db.read_only_connection() as conn:
            follower_before = dict(
                conn.execute(select(run_workers_table).where(run_workers_table.c.worker_id == follower_id)).mappings().one()
            )
        events_before = _coordination_events(db, run_id=run_id)

        repo.depart_worker(worker_id=follower_id, now=departed_at)

        with db.read_only_connection() as conn:
            follower_after = dict(
                conn.execute(select(run_workers_table).where(run_workers_table.c.worker_id == follower_id)).mappings().one()
            )
        expected_follower = dict(follower_before)
        expected_follower["status"] = "departed"
        expected_follower["departed_at"] = departed_at
        assert follower_after == expected_follower
        events_after_depart = _coordination_events(db, run_id=run_id)
        assert events_after_depart[:-1] == events_before
        depart_event = events_after_depart[-1]
        assert (
            depart_event["event_type"],
            depart_event["worker_id"],
            depart_event["leader_epoch"],
            depart_event["recorded_at"],
            depart_event["context_json"],
        ) == ("worker_depart", follower_id, None, departed_at, "{}")
        departed_image = capture_state_engine_image(db, run_id=run_id)

        snapshot = repo.worker_heartbeat(
            worker_id=follower_id,
            now=departed_at + timedelta(seconds=1),
            window_seconds=30,
        )

        assert snapshot.worker_active is False
        assert snapshot.worker_role == "follower"
        assert snapshot.leader_worker_id == leader_id
        assert capture_state_engine_image(db, run_id=run_id) == departed_image
        assert _coordination_events(db, run_id=run_id) == events_after_depart
    finally:
        db.close()


@pytest.mark.timeout(120)
def test_release_and_takeover_share_seat_then_membership_lock_order(postgres_url: str) -> None:
    """A release racing a takeover completes or loses silently, never deadlocks."""
    db = LandscapeDB.from_url(postgres_url)
    repo = RunCoordinationRepository(db.engine)
    with db.engine.begin() as conn:
        conn.execute(
            insert(runs_table).values(
                run_id=RUN_ID,
                started_at=NOW,
                config_hash="config",
                settings_json="{}",
                canonical_version="v1",
                status="failed",
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        )
    incumbent_id = mint_worker_id(RUN_ID)
    token = repo.register_run_leader(run_id=RUN_ID, worker_id=incumbent_id, now=NOW, window_seconds=1)
    successor_id = mint_worker_id(RUN_ID)

    release_has_first_lock = threading.Event()
    release_attempting_seat = threading.Event()
    acquire_attempting_seat = threading.Event()
    acquire_has_seat = threading.Event()
    allow_release = threading.Event()
    allow_acquire = threading.Event()
    release_lock_kind: list[str] = []
    outcomes: dict[str, object] = {}

    def before_sql(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        normalized = " ".join(statement.upper().split())
        name = threading.current_thread().name
        if name == "release" and normalized.startswith("UPDATE RUN_COORDINATION"):
            release_attempting_seat.set()
        elif name == "acquire" and normalized.startswith("UPDATE RUN_COORDINATION"):
            acquire_attempting_seat.set()

    def after_sql(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        normalized = " ".join(statement.upper().split())
        name = threading.current_thread().name
        if name == "release" and not release_lock_kind:
            if normalized.startswith("SELECT") and "FROM RUN_WORKERS" in normalized and "FOR UPDATE" in normalized:
                release_lock_kind.append("membership")
            elif normalized.startswith("UPDATE RUN_COORDINATION"):
                release_lock_kind.append("seat")
            if release_lock_kind:
                release_has_first_lock.set()
                if not allow_release.wait(timeout=30):
                    raise TimeoutError("release interleaving gate timed out")
        elif name == "acquire" and normalized.startswith("UPDATE RUN_COORDINATION"):
            acquire_has_seat.set()
            if not allow_acquire.wait(timeout=30):
                raise TimeoutError("acquire interleaving gate timed out")

    event.listen(db.engine, "before_cursor_execute", before_sql)
    event.listen(db.engine, "after_cursor_execute", after_sql)

    def release() -> None:
        try:
            repo.release_seat(token=token, now=NOW + timedelta(seconds=3))
            outcomes["release"] = "returned"
        except BaseException as exc:  # pragma: no cover - asserted below
            outcomes["release"] = exc

    def acquire() -> None:
        try:
            outcomes["acquire"] = repo.acquire_run_leadership(
                run_id=RUN_ID,
                worker_id=successor_id,
                now=NOW + timedelta(seconds=3),
                window_seconds=30,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            outcomes["acquire"] = exc

    release_thread = threading.Thread(target=release, name="release")
    acquire_thread = threading.Thread(target=acquire, name="acquire")
    try:
        release_thread.start()
        assert release_has_first_lock.wait(timeout=30), "release never acquired its first coordination lock"
        acquire_thread.start()
        assert acquire_attempting_seat.wait(timeout=30), "takeover never attempted its seat CAS"

        if release_lock_kind == ["membership"]:
            assert acquire_has_seat.wait(timeout=30), "takeover never acquired the seat behind membership-first release"
            allow_release.set()
            assert release_attempting_seat.wait(timeout=30), "release never attempted the seat behind takeover"
            allow_acquire.set()
        else:
            assert release_lock_kind == ["seat"]
            allow_release.set()
            assert acquire_has_seat.wait(timeout=30), "takeover never acquired the seat after release committed"
            allow_acquire.set()

        release_thread.join(timeout=60)
        acquire_thread.join(timeout=60)
        assert not release_thread.is_alive() and not acquire_thread.is_alive(), "coordination race threads wedged"
    finally:
        allow_release.set()
        allow_acquire.set()
        if release_thread.ident is not None:
            release_thread.join(timeout=30)
        if acquire_thread.ident is not None:
            acquire_thread.join(timeout=30)
        event.remove(db.engine, "before_cursor_execute", before_sql)
        event.remove(db.engine, "after_cursor_execute", after_sql)

    try:
        assert outcomes["release"] == "returned"
        acquired = outcomes["acquire"]
        assert isinstance(acquired, CoordinationToken), f"takeover returned {acquired!r}"
        assert acquired.worker_id == successor_id
        with db.engine.connect() as conn:
            seat = conn.execute(
                select(run_coordination_table.c.leader_worker_id, run_coordination_table.c.leader_epoch).where(
                    run_coordination_table.c.run_id == RUN_ID
                )
            ).one()
            workers: dict[str, str] = {
                str(row["worker_id"]): str(row["status"])
                for row in conn.execute(
                    select(run_workers_table.c.worker_id, run_workers_table.c.status).where(run_workers_table.c.run_id == RUN_ID)
                ).mappings()
            }
        assert tuple(seat) == (successor_id, 2)
        assert workers == {incumbent_id: "departed", successor_id: "active"}
    finally:
        db.close()
