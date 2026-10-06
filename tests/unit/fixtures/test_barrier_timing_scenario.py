"""The timing premise can retry; an actual recovery failure cannot."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from tests.e2e.recovery.test_barrier_timing_invariance import RUN_ID, _assert_with_stable_database_second, _usurp_seat
from tests.fixtures import landscape
from tests.fixtures.landscape import DatabaseSecondRollover

from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import run_coordination_table
from elspeth.engine.clock import MockClock


def test_rollover_discards_mutated_state_and_recreates_both_clocks() -> None:
    databases: list[LandscapeDB] = []
    clocks: list[MockClock] = []

    def scenario(db: LandscapeDB, factory: RecorderFactory, clock: MockClock) -> None:
        databases.append(db)
        clocks.append(clock)
        assert factory._db is db
        with db.engine.connect() as conn:
            epoch = conn.execute(
                select(run_coordination_table.c.leader_epoch).where(run_coordination_table.c.run_id == RUN_ID)
            ).scalar_one()
        assert epoch == 1
        if len(databases) == 1:
            _usurp_seat(db, RUN_ID, clock)
            clock.advance(5)
            raise DatabaseSecondRollover("forced clock-premise failure after mutation")

    _assert_with_stable_database_second(scenario)
    assert len(databases) == 2
    assert databases[0] is not databases[1]
    assert clocks[0] is not clocks[1]
    assert clocks[0].monotonic() - clocks[1].monotonic() == 5
    for db in databases:
        with pytest.raises(RuntimeError, match="Database not initialized"):
            _ = db.engine


@pytest.mark.parametrize("error", [AssertionError("broken recovery property"), RuntimeError("broken restore")])
def test_property_failures_propagate_without_retry(error: Exception) -> None:
    databases: list[LandscapeDB] = []

    def scenario(db: LandscapeDB, _factory: RecorderFactory, _clock: MockClock) -> None:
        databases.append(db)
        raise error

    with pytest.raises(type(error), match=str(error)):
        _assert_with_stable_database_second(scenario)
    assert len(databases) == 1
    with pytest.raises(RuntimeError, match="Database not initialized"):
        _ = databases[0].engine


def test_persistent_rollover_fails_after_bounded_fresh_attempts() -> None:
    databases: list[LandscapeDB] = []

    def scenario(db: LandscapeDB, _factory: RecorderFactory, _clock: MockClock) -> None:
        databases.append(db)
        raise DatabaseSecondRollover("persistent rollover")

    with pytest.raises(DatabaseSecondRollover, match="persistent rollover"):
        _assert_with_stable_database_second(scenario)
    assert len(databases) == 5
    assert len({id(db) for db in databases}) == 5
    for db in databases:
        with pytest.raises(RuntimeError, match="Database not initialized"):
            _ = db.engine


def test_boundary_helper_marks_only_post_action_rollover(monkeypatch: pytest.MonkeyPatch) -> None:
    before = datetime(2026, 10, 6, tzinfo=UTC)
    reads = iter([before, before + timedelta(seconds=1), before + timedelta(seconds=2)])
    monkeypatch.setattr(landscape, "landscape_database_now", lambda _engine: next(reads))
    calls: list[datetime] = []
    with pytest.raises(DatabaseSecondRollover, match="rolled over during the boundary action"):
        landscape.on_fresh_database_second(None, calls.append)
    assert calls == [before + timedelta(seconds=1)]
