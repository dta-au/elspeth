# tests/fixtures/landscape.py
"""Landscape database and RecorderFactory fixtures.

All fixtures are function-scoped for full test isolation.
No module-scoped databases — every test gets a fresh database.

Factory hierarchy:
    make_landscape_db()          → bare LandscapeDB
    make_factory()               → LandscapeDB + RecorderFactory
    make_recorder_with_run()     → LandscapeDB + RecorderFactory + run + source node
    register_test_node()         → add additional nodes to an existing run
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select

from elspeth.contracts import NodeType
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.payload_store import PayloadStore
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.query_repository import QueryRepository
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository
from elspeth.core.landscape.schema import run_workers_table
from tests.fixtures.stores import MockPayloadStore

# Shared default for schema_config across all factory-created nodes
_OBSERVED_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})


def make_landscape_db() -> LandscapeDB:
    """Factory for in-memory LandscapeDB."""
    return LandscapeDB.in_memory()


def expire_leader_seat(db: LandscapeDB, run_id: str) -> None:
    """Lapse the epoch-21 leader seat ``begin_run`` minted for ``run_id``.

    ADR-030 §B.4: a hard-killed leader never releases its ``run_coordination``
    seat — it stays HELD until the liveness window (80 s) lapses, and resume's
    takeover CAS requires vacant-or-expired. Fixtures that craft a crashed run
    via ``begin_run(...)`` + direct status writes (instead of running the real
    engine, whose ceremony arms release the seat) call this to produce the
    post-window image deterministically rather than sleeping out the window.

    The lapsed deadline is written relative to the Landscape database clock
    (ADR-047): the takeover CAS compares against that clock, never a process
    clock, so tests control time through the database.
    """
    from sqlalchemy import update

    from elspeth.core.landscape.database_clock import read_landscape_transaction_time
    from elspeth.core.landscape.schema import run_coordination_table

    with db.engine.begin() as conn:
        lapsed = read_landscape_transaction_time(conn) - timedelta(seconds=1)
        conn.execute(
            update(run_coordination_table).where(run_coordination_table.c.run_id == run_id).values(leader_heartbeat_expires_at=lapsed)
        )


def expire_worker(engine: Any, worker_id: str, *, seconds_ago: float = 1.0) -> None:
    """Lapse an active ``run_workers`` heartbeat ``seconds_ago`` seconds before database time.

    The liveness sweep (``dead_non_leader_workers`` / ``evict_worker``) judges
    ``heartbeat_expires_at < database_now - grace`` against the Landscape
    database clock (ADR-047); a test that needs a dead member writes the
    deadline into the database's past instead of handing the verb a clock.
    """
    from sqlalchemy import update

    from elspeth.core.landscape.database_clock import read_landscape_transaction_time

    with engine.begin() as conn:
        lapsed = read_landscape_transaction_time(conn) - timedelta(seconds=seconds_ago)
        conn.execute(update(run_workers_table).where(run_workers_table.c.worker_id == worker_id).values(heartbeat_expires_at=lapsed))


def expire_lease(engine: Any, work_item_id: str, *, seconds_ago: float = 1.0) -> datetime:
    """Age a LEASED work item's ``lease_expires_at`` to ``seconds_ago`` seconds before database time.

    The lease family (``recover_expired_leases``, ``heartbeat_lease``,
    ``peer_active_leases``, the claim CAS) decides expiry against the
    Landscape database clock (ADR-047); a test that needs an expired lease
    writes the deadline into the database's past instead of handing the verb
    a future clock. Refuses (``AssertionError``) unless exactly one LEASED
    row carries ``work_item_id``: ageing a row the sweep can never reap would
    turn the test into a no-op that still passes. Returns the deadline written.
    """
    from sqlalchemy import update

    from elspeth.contracts.scheduler import TokenWorkStatus
    from elspeth.core.landscape.database_clock import read_landscape_transaction_time
    from elspeth.core.landscape.schema import token_work_items_table

    with engine.begin() as conn:
        lapsed = read_landscape_transaction_time(conn) - timedelta(seconds=seconds_ago)
        result = conn.execute(
            update(token_work_items_table)
            .where(token_work_items_table.c.work_item_id == work_item_id)
            .where(token_work_items_table.c.status == TokenWorkStatus.LEASED.value)
            .values(lease_expires_at=lapsed)
        )
        if result.rowcount != 1:
            raise AssertionError(
                f"expire_lease: expected exactly one LEASED row for work_item_id={work_item_id!r}, matched {result.rowcount}"
            )
    return lapsed


def expire_sink_effect_lease(engine: Any, effect_id: str, *, seconds_ago: float = 1.0) -> datetime:
    """Age a sink effect's ``lease_expires_at`` to ``seconds_ago`` seconds before database time.

    The sink-effect lease family (``claim_preparation``, ``acquire_lease``,
    ``heartbeat_lease``, ``takeover_expired``, member results, finalization)
    decides liveness against the Landscape database clock (ADR-047, C6 stage
    4); a test that needs an expired effect lease writes the deadline into the
    database's past instead of advancing a process clock the repository no
    longer reads. Refuses (``AssertionError``) unless exactly one row carries
    ``effect_id`` with a non-NULL deadline: ageing a lease that does not exist
    would turn the test into a no-op that still passes. Returns the deadline
    written.
    """
    from sqlalchemy import update

    from elspeth.core.landscape.database_clock import read_landscape_transaction_time
    from elspeth.core.landscape.schema import sink_effects_table

    with engine.begin() as conn:
        lapsed = read_landscape_transaction_time(conn) - timedelta(seconds=seconds_ago)
        # ck_sink_effects_lease_window keeps lease_expires_at >= lease_heartbeat_at,
        # and the executor's wait refuses a non-positive window, so the last
        # heartbeat moves back with the deadline: a one-second window that
        # ended ``seconds_ago``.
        result = conn.execute(
            update(sink_effects_table)
            .where(sink_effects_table.c.effect_id == effect_id)
            .where(sink_effects_table.c.lease_expires_at.is_not(None))
            .values(lease_heartbeat_at=lapsed - timedelta(seconds=1), lease_expires_at=lapsed)
        )
        if result.rowcount != 1:
            raise AssertionError(
                f"expire_sink_effect_lease: expected exactly one leased sink effect for effect_id={effect_id!r}, matched {result.rowcount}"
            )
    return lapsed


def age_barrier_hold(engine: Any, work_item_id: str, *, seconds_ago: float) -> datetime:
    """Move a BLOCKED work item's ``barrier_blocked_at`` to ``seconds_ago`` seconds before database time.

    Barrier and coalesce restores anchor a hold's age at its durable
    ``barrier_blocked_at`` measured against the Landscape database clock
    (ADR-047, C6 stage 3); ``mark_blocked`` stamps that column from the
    database, so a test that needs an older hold writes the instant into the
    database's past instead of handing the disposition a clock. Refuses
    (``AssertionError``) unless exactly one BLOCKED row carries
    ``work_item_id``. Returns the instant written.
    """
    from sqlalchemy import update

    from elspeth.contracts.scheduler import TokenWorkStatus
    from elspeth.core.landscape.database_clock import read_landscape_transaction_time
    from elspeth.core.landscape.schema import token_work_items_table

    with engine.begin() as conn:
        blocked_at = read_landscape_transaction_time(conn) - timedelta(seconds=seconds_ago)
        result = conn.execute(
            update(token_work_items_table)
            .where(token_work_items_table.c.work_item_id == work_item_id)
            .where(token_work_items_table.c.status == TokenWorkStatus.BLOCKED.value)
            .values(barrier_blocked_at=blocked_at)
        )
        if result.rowcount != 1:
            raise AssertionError(
                f"age_barrier_hold: expected exactly one BLOCKED row for work_item_id={work_item_id!r}, matched {result.rowcount}"
            )
    return blocked_at


def await_database_time(engine: Any, instant: datetime, *, timeout_seconds: float = 5.0) -> datetime:
    """Block until the Landscape database clock is strictly past ``instant``; return the clock read.

    For the few tests whose audit witness must stay intact — source-completion
    reconciliation compares the row's ``lease_expires_at`` with the CLAIM_READY
    event's ``to_lease_expires_at`` — a lease can only expire the way it does
    in production: by database time passing. Pair with a one-second lease so
    the wait is bounded by two whole SQLite seconds.
    """
    import time

    deadline = time.monotonic() + timeout_seconds
    while True:
        database_now = landscape_database_now(engine)
        if database_now > instant:
            return database_now
        if time.monotonic() > deadline:
            raise AssertionError(
                f"Landscape database time {database_now.isoformat()} did not pass {instant.isoformat()} within {timeout_seconds} s"
            )
        time.sleep(0.05)


def reschedule_work_item(engine: Any, work_item_id: str, *, seconds_from_now: float) -> datetime:
    """Move a work item's ``available_at`` to ``seconds_from_now`` seconds from database time.

    ``claim_ready`` admits a READY row only once ``available_at <= database_now``
    (ADR-047). A test that needs a row parked in the future, or released into
    the past, writes that deadline through the database rather than handing
    the claim a clock. Refuses unless exactly one row carries
    ``work_item_id``. Returns the ``available_at`` written.
    """
    from sqlalchemy import update

    from elspeth.core.landscape.database_clock import read_landscape_transaction_time
    from elspeth.core.landscape.schema import token_work_items_table

    with engine.begin() as conn:
        available_at = read_landscape_transaction_time(conn) + timedelta(seconds=seconds_from_now)
        result = conn.execute(
            update(token_work_items_table).where(token_work_items_table.c.work_item_id == work_item_id).values(available_at=available_at)
        )
        if result.rowcount != 1:
            raise AssertionError(
                f"reschedule_work_item: expected exactly one row for work_item_id={work_item_id!r}, matched {result.rowcount}"
            )
    return available_at


def on_fresh_database_second(engine: Any, action: Any) -> Any:
    """Run ``action(database_now)`` just after a database-second boundary and return its result.

    The exact-boundary arm of a lease decision ("a deadline EQUAL to database
    time is not yet expired") can only be pinned on SQLite's whole-second
    clock when the deadline is written and the verb decides inside the same
    database second. Unlike :func:`within_one_database_second` this does not
    retry — the action is expected to mutate state — but starting at the top
    of a fresh second leaves the whole second for the write and the verb, and
    a rollover during the action is reported as a failure rather than
    silently scored.
    """
    import time

    first = landscape_database_now(engine)
    deadline = time.monotonic() + 3.0
    while (before := landscape_database_now(engine)) == first:
        if time.monotonic() > deadline:
            raise AssertionError("the Landscape database second did not advance within 3 s")
        time.sleep(0.002)
    result = action(before)
    after = landscape_database_now(engine)
    if after != before:
        raise AssertionError(
            f"the Landscape database second rolled over during the boundary action ({before.isoformat()} -> {after.isoformat()})"
        )
    return result


def late_in_a_database_second(engine: Any, action: Any, *, fraction: float = 0.95) -> Any:
    """Run ``action(database_now)`` in the last sliver of a database second and return its result.

    The mirror of :func:`on_fresh_database_second`, and the corner where a
    quantised clock is least forgiving: a deadline stamped as
    ``database_now + ttl`` from here discards almost a whole second of the
    stamp instant, so a lease that should live for ``ttl`` lapses at the
    boundary a few tens of milliseconds away. Any lever that stamps a deadline
    and then asserts it survives a real interval belongs here rather than at
    the top of a second, where the same defect is invisible.
    """
    import time

    if not 0.0 < fraction < 1.0:
        raise ValueError("fraction must sit strictly inside one second")
    first = landscape_database_now(engine)
    deadline = time.monotonic() + 3.0
    while (before := landscape_database_now(engine)) == first:
        if time.monotonic() > deadline:
            raise AssertionError("the Landscape database second did not advance within 3 s")
        time.sleep(0.002)
    # ``before`` is the first read of a fresh second; hold until ``fraction``
    # of that second has passed, then act.
    boundary = time.monotonic()
    while time.monotonic() - boundary < fraction:
        time.sleep(0.002)
    return action(before)


def within_one_database_second(engine: Any, action: Any, *, attempts: int = 20) -> Any:
    """Run ``action(database_now)`` and return its result once it completed inside one database second.

    SQLite's ``CURRENT_TIMESTAMP`` is whole-second, so an exact-boundary arm
    ("a deadline EQUAL to database time minus the grace is not yet expired")
    can only be pinned when the row is seeded and the verb decides within the
    same database second. ``action`` receives the database time read just
    before it ran and must be safe to repeat: when the clock rolled over
    during the attempt the result is untrustworthy and the action runs again.
    """
    for _ in range(attempts):
        before = landscape_database_now(engine)
        result = action(before)
        if landscape_database_now(engine) == before:
            return result
    raise AssertionError(f"the Landscape database second rolled over during every one of {attempts} attempts")


@contextmanager
def stamp_inside_next_transaction(engine: Any, statement: Any) -> Iterator[None]:
    """Run ``statement`` as the FIRST statement of the next transaction begun on ``engine``.

    PostgreSQL's ``CURRENT_TIMESTAMP`` is transaction time, so a deadline the
    statement writes from ``func.current_timestamp()`` equals, to the
    microsecond, the ``database_now`` the production verb reads inside that
    same transaction — the only way to pin an exact-boundary arm ("a deadline
    EQUAL to database time is not yet expired") against a microsecond clock.
    The stamp shares the verb's transaction, so a refused verb rolls it back
    with everything else. One-shot: the block must begin exactly one
    transaction on ``engine`` before any other.
    """
    from sqlalchemy import event

    fired: list[bool] = []

    def stamp(conn: Any) -> None:
        if fired:
            return
        fired.append(True)
        conn.execute(statement)

    event.listen(engine, "begin", stamp)
    try:
        yield
    finally:
        event.remove(engine, "begin", stamp)
    assert fired, "no transaction began on the engine while the boundary stamp was armed"


def insert_crashed_leader_seat(conn: Any, *, run_id: str) -> None:
    """Insert the expired ``run_coordination`` seat row a crashed leader leaves.

    For fixtures that craft the ``runs`` row via raw SQL (bypassing
    ``begin_run``, which at epoch 21 mints the seat atomically with the run):
    without a seat row, resume's takeover CAS refuses with
    ``AuditIntegrityError`` ("no run_coordination seat row"). Call on the same
    connection/transaction that inserted the ``runs`` row (FK). The lapsed
    deadline is relative to the Landscape database clock (ADR-047).
    """
    from sqlalchemy import insert as sa_insert

    from elspeth.core.landscape.database_clock import read_landscape_transaction_time
    from elspeth.core.landscape.schema import run_coordination_table

    lapsed = read_landscape_transaction_time(conn) - timedelta(seconds=1)
    conn.execute(
        sa_insert(run_coordination_table).values(
            run_id=run_id,
            leader_worker_id=f"worker:{run_id}:crashed-leader",
            leader_epoch=1,
            leader_heartbeat_expires_at=lapsed,
            updated_at=lapsed,
        )
    )


def leader_token_for(db: LandscapeDB, run_id: str) -> CoordinationToken:
    """The run's OWN leader token, read back from its ``run_coordination`` seat.

    ADR-048 §5: this is the one sanctioned way for a test to obtain a token
    for a run it did not mint through the production path. ``begin_run``
    always mints the seat (self-minted worker identity at epoch 1); reading
    it back proves the seat exists, so a threading defect that deposed the
    writer fails the test instead of being papered over by a self-minted
    token. The fence predicate is identity+epoch only — an expired seat still
    passes its own leader's fence.
    """

    leader = RunCoordinationRepository(db.engine).live_leader(run_id=run_id)
    if leader is None:
        raise AssertionError(f"run {run_id!r} has no run_coordination seat; begin_run mints one — was the run created via raw SQL?")
    return CoordinationToken(run_id=run_id, worker_id=leader.leader_worker_id, leader_epoch=leader.leader_epoch)


def leader_coordination_token(factory: RecorderFactory, run_id: str) -> CoordinationToken:
    """The run's OWN leader token through the factory (see :func:`leader_token_for`).

    ADR-030 slice 3: the journal-first barrier intake's adoption verbs are
    leader-fenced with NO unfenced arm, so any test that drives barrier work
    through a directly-constructed ``RowProcessor`` must bind the coordination
    token.
    """

    leader = factory.run_coordination.live_leader(run_id=run_id)
    if leader is None:
        raise AssertionError(f"run {run_id!r} has no run_coordination seat; begin_run mints one — was the run created via raw SQL?")
    return CoordinationToken(run_id=run_id, worker_id=leader.leader_worker_id, leader_epoch=leader.leader_epoch)


def register_test_worker(
    db: LandscapeDB,
    *,
    run_id: str,
    worker_id: str,
    heartbeat_expires_at: datetime | None = None,
) -> None:
    """Register an active test worker so fenced scheduler claim verbs can use it."""
    registered_at = datetime.now(UTC)
    with db.engine.begin() as conn:
        exists = conn.execute(
            select(run_workers_table.c.worker_id)
            .where(run_workers_table.c.worker_id == worker_id)
            .where(run_workers_table.c.run_id == run_id)
        ).first()
        if exists is not None:
            return
        conn.execute(
            insert(run_workers_table).values(
                worker_id=worker_id,
                run_id=run_id,
                role="follower",
                status="active",
                registered_at=registered_at,
                heartbeat_expires_at=heartbeat_expires_at or registered_at + timedelta(hours=1),
            )
        )


def make_factory(db: LandscapeDB | None = None, *, payload_store: PayloadStore | None = None) -> RecorderFactory:
    """Factory for RecorderFactory.

    Always wires a payload store so expand_token / coalesce_tokens can
    persist per-token payloads (required since epoch 11). Tests that don't
    care about the stored bytes get a fresh MockPayloadStore automatically.
    Pass an explicit payload_store to inspect stored payloads in assertions.
    """
    if db is None:
        db = make_landscape_db()
    if payload_store is None:
        payload_store = MockPayloadStore()
    return RecorderFactory(db, payload_store=payload_store)


# =============================================================================
# RecorderSetup — The 80% setup pattern as a single factory call
# =============================================================================


@dataclass
class RecorderSetup:
    """Result from make_recorder_with_run().

    Plain @dataclass — test scaffolding, not audit records.
    Note: db and factory are mutable objects; frozen=True would only prevent
    reference reassignment without providing an immutability guarantee.
    """

    db: LandscapeDB
    factory: RecorderFactory
    run_id: str
    source_node_id: str
    # The run's epoch-1 leader token, read back from the seat begin_run
    # minted (ADR-048 §5) — what every fenced verb under test presents.
    coordination_token: CoordinationToken

    @property
    def run_lifecycle(self) -> RunLifecycleRepository:
        return self.factory.run_lifecycle

    @property
    def execution(self) -> ExecutionRepository:
        return self.factory.execution

    @property
    def data_flow(self) -> DataFlowRepository:
        return self.factory.data_flow

    @property
    def query(self) -> QueryRepository:
        return self.factory.query


def make_recorder_with_run(
    *,
    run_id: str | None = None,
    source_node_id: str | None = None,
    source_plugin_name: str = "source",
    canonical_version: str = "v1",
    payload_store: PayloadStore | None = None,
    leader_worker_id: str | None = None,
) -> RecorderSetup:
    """Create LandscapeDB + RecorderFactory + run + source node in one call.

    Covers the 80% setup pattern: db → factory → begin_run → register_node(SOURCE).
    Tests needing additional nodes (transforms, sinks, aggregations) can call
    factory.data_flow.register_node() on the returned factory, or use register_test_node().

    Always call this inside individual test methods or setup_method(), never
    setup_class(). It creates a fresh in-memory DB per call for test isolation.

    Args:
        run_id: Explicit run ID for deterministic tests. Auto-generated if None.
        source_node_id: Explicit source node ID. Auto-generated if None.
        source_plugin_name: Plugin name for the source node (default "source").
        canonical_version: Version string for begin_run (default "v1").
            Some tests (e.g., test_processor.py) use "sha256-rfc8785-v1".
        payload_store: Payload store to inject (defaults to MockPayloadStore).
            Pass a specific MockPayloadStore instance to inspect stored payloads.
            Tests that explicitly test "no payload store" behavior must pass
            RecorderFactory(db) directly rather than using this helper.
        leader_worker_id: Optional registered leader worker identity. Tests that
            drive fenced scheduler claim verbs with a fixed lease owner should
            pass that same value here.
    """
    db = make_landscape_db()
    factory = make_factory(db, payload_store=payload_store)

    # Build kwargs, only passing explicit IDs if provided
    begin_kwargs: dict[str, Any] = {
        "config": {},
        "canonical_version": canonical_version,
    }
    if run_id is not None:
        begin_kwargs["run_id"] = run_id
    if leader_worker_id is not None:
        begin_kwargs["leader_worker_id"] = leader_worker_id

    run = factory.run_lifecycle.begin_run(**begin_kwargs)

    register_kwargs: dict[str, Any] = {
        "run_id": run.run_id,
        "plugin_name": source_plugin_name,
        "node_type": NodeType.SOURCE,
        "plugin_version": "1.0",
        "config": {},
        "schema_config": _OBSERVED_SCHEMA,
    }
    if source_node_id is not None:
        register_kwargs["node_id"] = source_node_id

    node = factory.data_flow.register_node(**register_kwargs)

    setup = RecorderSetup(
        db=db,
        factory=factory,
        run_id=run.run_id,
        source_node_id=node.node_id,
        coordination_token=leader_coordination_token(factory, run.run_id),
    )

    # Offensive programming: verify round-trip invariant.
    # If this assertion fails, the factory itself is broken.
    assert setup.run_id == run.run_id, f"Factory bug: returned run_id {setup.run_id!r} != begin_run result {run.run_id!r}"
    assert setup.source_node_id == node.node_id, (
        f"Factory bug: returned source_node_id {setup.source_node_id!r} != register_node result {node.node_id!r}"
    )

    return setup


def register_test_node(
    data_flow: DataFlowRepository,
    run_id: str,
    node_id: str,
    *,
    node_type: NodeType = NodeType.TRANSFORM,
    plugin_name: str = "transform",
) -> str:
    """Register an additional test node with sensible defaults.

    For the 20% variant pattern where tests need 2-5 additional nodes
    after make_recorder_with_run() creates the source.

    Defaults plugin_version="1.0", config={}, schema_config=observed.
    Returns the node_id for convenience.
    """
    node = data_flow.register_node(
        run_id=run_id,
        plugin_name=plugin_name,
        node_type=node_type,
        plugin_version="1.0",
        config={},
        node_id=node_id,
        schema_config=_OBSERVED_SCHEMA,
    )
    return node.node_id


# =============================================================================
# Pytest fixtures
# =============================================================================


@pytest.fixture
def landscape_db() -> LandscapeDB:
    """Function-scoped in-memory LandscapeDB — fresh per test."""
    return make_landscape_db()


@pytest.fixture
def landscape_factory(landscape_db: LandscapeDB) -> RecorderFactory:
    """Function-scoped RecorderFactory."""
    return RecorderFactory(landscape_db)


@pytest.fixture
def landscape_factory_with_payload_store(landscape_db: LandscapeDB, tmp_path: Any) -> RecorderFactory:
    """RecorderFactory with real filesystem payload store."""
    from elspeth.core.payload_store import FilesystemPayloadStore

    payload_dir = tmp_path / "payloads"
    payload_store = FilesystemPayloadStore(payload_dir)
    return RecorderFactory(landscape_db, payload_store=payload_store)


def landscape_database_now(engine: Any) -> datetime:
    """Read the Landscape database clock once, outside any decision transaction.

    Test-side control of time goes through the database (ADR-047): compare a
    deadline the production writer stamped against this value, never against
    ``datetime.now``. SQLite's ``CURRENT_TIMESTAMP`` is whole-second UTC, so
    pair it with :func:`assert_deadline_within`.
    """
    from elspeth.core.landscape.database_clock import read_landscape_transaction_time

    with engine.connect() as conn:
        return read_landscape_transaction_time(conn)


def assert_deadline_within(actual: datetime, expected: datetime, *, tolerance: timedelta = timedelta(seconds=1)) -> None:
    """Assert a database-stamped deadline equals ``expected`` within the clock's resolution.

    SQLite stamps whole seconds with no fraction and PostgreSQL stamps
    microseconds; a value read back naive is the storage's UTC.
    """
    actual_utc = actual if actual.tzinfo is not None else actual.replace(tzinfo=UTC)
    expected_utc = expected if expected.tzinfo is not None else expected.replace(tzinfo=UTC)
    assert abs(actual_utc - expected_utc) <= tolerance, (
        f"deadline {actual_utc.isoformat()} is not within {tolerance} of {expected_utc.isoformat()}"
    )


def assert_stamped_between(
    actual: datetime,
    *,
    start: datetime,
    end: datetime,
    offset: timedelta = timedelta(0),
    tolerance: timedelta = timedelta(seconds=1),
) -> None:
    """Assert a database-stamped value lies in ``[start + offset, end + offset]`` within the clock's resolution.

    Bracket a production verb with two :func:`landscape_database_now` reads
    and hand them in as ``start`` / ``end``: the stamp the verb wrote must
    fall between them (plus ``offset`` for a deadline), whatever the box's
    load did to the verb's wall-clock duration.
    """
    actual_utc = actual if actual.tzinfo is not None else actual.replace(tzinfo=UTC)
    lower = (start if start.tzinfo is not None else start.replace(tzinfo=UTC)) + offset - tolerance
    upper = (end if end.tzinfo is not None else end.replace(tzinfo=UTC)) + offset + tolerance
    assert lower <= actual_utc <= upper, f"stamp {actual_utc.isoformat()} is outside [{lower.isoformat()}, {upper.isoformat()}]"
