"""PostgreSQL proof that the checkpoint fence serialises on the seat row.

The property the D8 fencing programme rests on is a ROW-LOCK property: the
leader fence's first statement (``verify_and_extend_leader_fence``) takes the
run's ``run_coordination`` seat row for the whole payload transaction, so two
fenced verbs on the same run cannot both be past the fence at once.

SQLite cannot express it. It has no ``FOR UPDATE`` and no row-level lock
waiting, so a serialised SQLite run and an unserialised one are
indistinguishable — a unit-level stale-token refusal is not weak evidence of
this property, it is none. Both checkpoint verbs moved onto the fence in this
lane (ADR-048 §2) and had no PostgreSQL witness for it until here.

The assertion is POSITIVE and mechanism-level rather than timing-based: while
``create_checkpoint`` is held inside its fenced transaction, the competing
``delete_checkpoints`` backend must appear in ``pg_blocking_pids()`` as blocked
BY the holder. With the fence removed neither verb touches ``run_coordination``,
no backend ever blocks, and this test fails on its own timeout — which is the
negative it is required to be able to return.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from tests.fixtures.landscape import leader_coordination_token, make_factory
from tests.helpers.checkpoint import checkpoint_draft
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import NodeType
from elspeth.contracts.barrier_scalars import AggregationNodeScalars, BarrierScalars
from elspeth.core.checkpoint import manager as checkpoint_manager_module
from elspeth.core.checkpoint.manager import CheckpointManager
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import checkpoints_table

pytestmark = pytest.mark.testcontainer

NOW = datetime(2026, 7, 24, tzinfo=UTC)


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


@pytest.fixture
def postgres_db(postgres_url: str) -> Iterator[LandscapeDB]:
    db = LandscapeDB(postgres_url)
    try:
        yield db
    finally:
        db.close()


def _graph() -> ExecutionGraph:
    graph = ExecutionGraph()
    graph.add_node("transform", node_type=NodeType.TRANSFORM, plugin_name="test", config={})
    return graph


def _scalars() -> BarrierScalars:
    return BarrierScalars(
        aggregation={"agg": AggregationNodeScalars(count_fire_offset=1.0, condition_fire_offset=None)},
        coalesce={},
    )


def _seat_row_is_locked(db: LandscapeDB) -> bool:
    """Is SOME backend holding an uncommitted write lock on ``run_coordination``?

    Anti-vacuity guard, and it guards the failure mode that actually threatens
    this test. Waiting on the hook's event proves the hook FIRED; it does not
    prove it fired INSIDE the fence. If ``checkpoint_dumps`` is ever moved out
    of the fenced block, the event still fires, nothing holds the seat, and the
    test would go on to measure nothing.

    Asked from an observer connection rather than from inside the hook, because
    the hook receives only the payload and never the fenced connection — the
    same absence that made the first version of this test read a pid from the
    wrong backend.
    """
    with db.engine.connect() as observer:
        held = observer.exec_driver_sql(
            "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
            "WHERE l.granted AND l.mode = 'RowExclusiveLock' AND c.relname = 'run_coordination'"
        ).scalar_one()
        return int(held) > 0


def _backends_blocked_on_the_seat(db: LandscapeDB) -> list[tuple[int, str]]:
    """Backends PostgreSQL reports as blocked while executing the fence statement.

    Identified by the statement they are stuck on, not by a pid captured
    elsewhere: the fence's verify-and-extend is the only thing either checkpoint
    verb runs against ``run_coordination``, so a backend blocked on that
    statement is a backend that has NOT passed the fence.
    """
    with db.engine.connect() as observer:
        rows = observer.exec_driver_sql(
            "SELECT pid, query FROM pg_stat_activity "
            "WHERE pid <> pg_backend_pid() "
            "AND cardinality(pg_blocking_pids(pid)) > 0 "
            "AND query ILIKE '%%run_coordination%%'"
        ).all()
        return [(int(pid), str(query)) for pid, query in rows]


def test_two_fenced_checkpoint_verbs_serialise_on_the_seat_row(
    postgres_db: LandscapeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``delete_checkpoints`` cannot pass the fence while ``create_checkpoint`` holds it.

    Both verbs are leader-fenced with no unfenced arm, so the seat row is the
    single point they contend on. PostgreSQL is asked directly whether the
    second backend is blocked by the first, rather than inferred from elapsed
    time.
    """
    db = postgres_db
    creator = make_factory(db)
    deleter = make_factory(db)
    run = creator.run_lifecycle.begin_run(config={}, canonical_version="v1")
    token = leader_coordination_token(creator, run.run_id)

    # A prior checkpoint so the competing delete has a row to remove and its
    # success is observable rather than vacuous.
    CheckpointManager(db).create_checkpoint(
        draft=checkpoint_draft(run_id=run.run_id, sequence_number=0, graph=_graph()),
        coordination_token=token,
    )

    holder_is_past_the_fence = threading.Event()
    release_holder = threading.Event()
    original_dumps = checkpoint_manager_module.checkpoint_dumps

    def dumps_inside_the_fenced_transaction(payload: object) -> str:
        # Reached only INSIDE create_checkpoint's fenced transaction, so this
        # thread's backend already holds the seat row when we signal. We do NOT
        # try to capture that backend's pid here: this hook does not receive the
        # fenced connection, and a pid read from a fresh connection would be a
        # different backend entirely.
        holder_is_past_the_fence.set()
        assert release_holder.wait(timeout=20)
        return original_dumps(payload)

    monkeypatch.setattr(checkpoint_manager_module, "checkpoint_dumps", dumps_inside_the_fenced_transaction)

    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(
            CheckpointManager(db).create_checkpoint,
            draft=checkpoint_draft(run_id=run.run_id, sequence_number=1, graph=_graph(), barrier_scalars=_scalars()),
            coordination_token=token,
        )
        assert holder_is_past_the_fence.wait(timeout=20), "create_checkpoint never reached its fenced transaction"

        # ANTI-VACUITY: the event above proves only that the hook fired. Prove
        # it fired INSIDE the fence, by asking PostgreSQL whether the seat row
        # is actually held right now. Relocating checkpoint_dumps out of the
        # fenced block would still fire the event and would fail HERE, loudly,
        # instead of leaving the rest of this test measuring nothing.
        assert _seat_row_is_locked(db), (
            "the hook fired but no backend holds a write lock on run_coordination: "
            "checkpoint_dumps is no longer inside the fenced transaction, so this test "
            "would be asserting serialisation against a fence nobody is holding"
        )

        deleting = pool.submit(
            CheckpointManager(db).delete_checkpoints,
            coordination_token=leader_coordination_token(deleter, run.run_id),
        )

        # POSITIVE assertion: PostgreSQL reports a backend BLOCKED on the fence
        # statement while the holder is inside its fenced transaction. Without
        # the fence neither verb touches run_coordination at all, nothing ever
        # blocks, and this loop times out — the negative this test must be able
        # to return.
        blocked: list[tuple[int, str]] = []
        settle = threading.Event()
        for _ in range(200):
            blocked = _backends_blocked_on_the_seat(db)
            if blocked:
                break
            settle.wait(0.05)
        assert blocked, (
            "no backend blocked on the run_coordination seat: delete_checkpoints passed the fence "
            "while create_checkpoint held it for the same run"
        )
        assert not creating.done(), "the holder released before the competitor was observed blocking"

        release_holder.set()
        creating.result(timeout=20)
        deleting.result(timeout=20)

    with db.read_only_connection() as conn:
        remaining = conn.scalar(select(func.count()).select_from(checkpoints_table).where(checkpoints_table.c.run_id == run.run_id))
    assert remaining == 0, "the delete ran after the create released the seat, so it removed both checkpoints"
