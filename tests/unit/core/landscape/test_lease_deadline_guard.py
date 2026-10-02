"""Fresh decision samples and transaction-scoped deadline completion."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, event, select
from sqlalchemy.engine import Connection, Engine

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import begin_write
from elspeth.core.landscape.database_clock import read_landscape_decision_time
from elspeth.core.landscape.journal import JournalRecord, LandscapeJournal
from elspeth.core.landscape.lease_deadlines import (
    DeadlineKey,
    DeadlineKind,
    LeaseDeadlineExpiredError,
    forget_issued_deadline,
    has_issued_deadline,
    install_deadline_guard,
    record_issued_deadline,
)
from elspeth.core.landscape.schema import sidecar_journal_outbox_table

_NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
_KEY = DeadlineKey(DeadlineKind.ITEM, ("item",))
_PROBE = Table("deadline_probe", MetaData(), Column("id", Integer, primary_key=True))


@pytest.mark.parametrize(
    "kind, identity",
    [
        (DeadlineKind.LEADER, ("run", "worker", "2")),
        (DeadlineKind.WORKER, ("run", "worker")),
        (DeadlineKind.ITEM, ("item",)),
        (DeadlineKind.SINK_EFFECT, ("effect",)),
    ],
)
def test_deadline_keys_are_hashable_full_identities(kind: DeadlineKind, identity: tuple[str, ...]) -> None:
    key = DeadlineKey(kind, identity)
    assert {key: "issued"}[DeadlineKey(kind, identity)] == "issued"
    if kind is DeadlineKind.LEADER:
        assert DeadlineKey(kind, ("run", "worker", "3")) not in {key: "issued"}


@pytest.mark.parametrize("identity", ["x", ["x"], {"x"}, frozenset({"x"}), {"x": "value"}])
def test_deadline_key_rejects_other_iterable_carriers(identity: object) -> None:
    # A one-element iterable can pass arity/member checks while violating the
    # immutable tuple key contract. A freezing call alone cannot validate it.
    with pytest.raises(ValueError, match="identity requires"):
        DeadlineKey(DeadlineKind.ITEM, identity)


@pytest.mark.parametrize("identity", [(), ("x", "y"), ("",), (None,), (3,), (["x"],)])
def test_deadline_key_rejects_wrong_arity_and_non_string_members(identity: tuple[object, ...]) -> None:
    with pytest.raises(ValueError, match="identity requires"):
        DeadlineKey(DeadlineKind.ITEM, identity)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'guard.db'}", pool_size=1, max_overflow=0)
    _PROBE.create(engine)
    sidecar_journal_outbox_table.create(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_decision_clock_reads_and_normalizes_database_result(dialect: str) -> None:
    conn = Mock(spec=Connection, dialect=SimpleNamespace(name=dialect))
    conn.scalar.return_value = _NOW.astimezone(timezone(timedelta(hours=10))) if dialect == "postgresql" else _NOW.replace(tzinfo=None)
    assert read_landscape_decision_time(conn) == _NOW
    assert conn.scalar.call_count == 1
    sql = str(conn.scalar.call_args.args[0])
    assert ("clock_timestamp" if dialect == "postgresql" else "strftime") in sql


@pytest.mark.parametrize(
    "dialect, value", [("postgresql", None), ("postgresql", _NOW.replace(tzinfo=None)), ("sqlite", _NOW), ("sqlite", "2026-09-08")]
)
def test_decision_clock_rejects_corrupt_results(dialect: str, value: object) -> None:
    conn = Mock(spec=Connection, dialect=SimpleNamespace(name=dialect))
    conn.scalar.return_value = value
    with pytest.raises(AuditIntegrityError):
        read_landscape_decision_time(conn)


def test_decision_clock_rejects_unknown_dialect() -> None:
    conn = Mock(spec=Connection, dialect=SimpleNamespace(name="mysql"))
    with pytest.raises(NotImplementedError):
        read_landscape_decision_time(conn)
    conn.scalar.assert_not_called()


@pytest.mark.parametrize("remaining, refused", [(1.000001, False), (1.0, True), (0.999999, True)])
def test_reserve_boundary_and_rollback_on_same_pooled_connection(engine: Engine, remaining: float, refused: bool) -> None:
    with patch("elspeth.core.landscape.lease_deadlines.read_landscape_decision_time", return_value=_NOW):
        if refused:
            with pytest.raises(LeaseDeadlineExpiredError), begin_write(engine) as conn:
                conn.execute(_PROBE.insert().values(id=1))
                record_issued_deadline(conn, key=_KEY, expires_at=_NOW + timedelta(seconds=remaining), window_seconds=80.0)
        else:
            with begin_write(engine) as conn:
                conn.execute(_PROBE.insert().values(id=1))
                record_issued_deadline(conn, key=_KEY, expires_at=_NOW + timedelta(seconds=remaining), window_seconds=80.0)
        with begin_write(engine) as conn:
            assert not has_issued_deadline(conn, key=_KEY)
            conn.execute(_PROBE.insert().values(id=2))
        with engine.connect() as conn:
            assert list(conn.scalars(select(_PROBE.c.id).order_by(_PROBE.c.id))) == ([2] if refused else [1, 2])


def test_replacement_and_forget_are_specific_to_exact_key(engine: Engine) -> None:
    other = DeadlineKey(DeadlineKind.WORKER, ("run", "worker"))
    with patch("elspeth.core.landscape.lease_deadlines.read_landscape_decision_time", return_value=_NOW), engine.begin() as conn:
        record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=10.0)
        record_issued_deadline(conn, key=other, expires_at=_NOW, window_seconds=10.0)
        record_issued_deadline(conn, key=_KEY, expires_at=_NOW + timedelta(seconds=10), window_seconds=10.0)
        forget_issued_deadline(conn, key=other)
        assert has_issued_deadline(conn, key=_KEY)
        assert not has_issued_deadline(conn, key=other)
        conn.execute(_PROBE.insert().values(id=1))
    with engine.connect() as conn:
        assert list(conn.scalars(select(_PROBE.c.id))) == [1]


def test_refused_commit_rolls_back_before_caller_reuses_open_connection(engine: Engine) -> None:
    with engine.connect() as conn, patch("elspeth.core.landscape.lease_deadlines.read_landscape_decision_time", return_value=_NOW):
        with pytest.raises(LeaseDeadlineExpiredError), conn.begin():
            conn.execute(_PROBE.insert().values(id=1))
            record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=10)
        with conn.begin():
            conn.execute(_PROBE.insert().values(id=2))
    with engine.connect() as conn:
        assert list(conn.scalars(select(_PROBE.c.id).order_by(_PROBE.c.id))) == [2]


def test_no_obligation_does_not_read_clock(engine: Engine) -> None:
    install_deadline_guard(engine)
    with patch("elspeth.core.landscape.lease_deadlines.read_landscape_decision_time", side_effect=AssertionError("unexpected clock read")):
        with engine.begin() as conn:
            assert not has_issued_deadline(conn, key=_KEY)
        with engine.begin() as conn:
            record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=10)
            forget_issued_deadline(conn, key=_KEY)


def test_forgetting_unissued_key_preserves_other_obligation(engine: Engine) -> None:
    absent = DeadlineKey(DeadlineKind.WORKER, ("run", "worker"))
    with patch("elspeth.core.landscape.lease_deadlines.read_landscape_decision_time", return_value=_NOW), engine.begin() as conn:
        record_issued_deadline(conn, key=_KEY, expires_at=_NOW + timedelta(seconds=10), window_seconds=10)
        forget_issued_deadline(conn, key=absent)
        forget_issued_deadline(conn, key=absent)
        assert has_issued_deadline(conn, key=_KEY)


def test_rollback_and_savepoint_do_not_leak_obligations(engine: Engine) -> None:
    with pytest.raises(RuntimeError, match="body failure"), begin_write(engine) as conn:
        record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=10)
        raise RuntimeError("body failure")
    with engine.begin() as conn:
        assert not has_issued_deadline(conn, key=_KEY)
        with conn.begin_nested(), pytest.raises(RuntimeError, match="savepoint"):
            record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=10)
        assert not has_issued_deadline(conn, key=_KEY)


@pytest.mark.parametrize("window", [0.0, -1.0, float("inf"), float("nan"), 1e-20, 0.001])
def test_rejects_invalid_window(engine: Engine, window: float) -> None:
    with engine.begin() as conn, pytest.raises(ValueError):
        record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=window)


def test_rejects_non_utc_expiry_and_no_transaction(engine: Engine) -> None:
    with engine.connect() as conn:
        with pytest.raises(RuntimeError, match="transaction"):
            record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=10)
        with conn.begin(), pytest.raises(ValueError, match="UTC"):
            record_issued_deadline(conn, key=_KEY, expires_at=_NOW.replace(tzinfo=None), window_seconds=10)


def test_guard_follows_connection_listener_and_late_attached_journal(engine: Engine, tmp_path: Path) -> None:
    install_deadline_guard(engine)
    journal = LandscapeJournal(str(tmp_path / "journal.jsonl"), fail_on_error=True)
    journal.attach(engine)
    order: list[str] = []
    serialize = journal._serialize_record

    def serialization(record: JournalRecord) -> str:
        order.append("journal")
        return serialize(record)

    def sample(conn: object) -> datetime:
        order.append("guard")
        assert order == ["connection", "journal", "guard"]
        return _NOW

    with (
        patch.object(journal, "_serialize_record", side_effect=serialization),
        patch("elspeth.core.landscape.lease_deadlines.read_landscape_decision_time", side_effect=sample),
        pytest.raises(LeaseDeadlineExpiredError),
        engine.begin() as conn,
    ):
        event.listen(conn, "commit", lambda connection: order.append("connection"))
        conn.execute(_PROBE.insert().values(id=1))
        record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=10)
    with engine.connect() as conn:
        assert list(conn.scalars(select(_PROBE.c.id))) == []
        assert conn.execute(select(sidecar_journal_outbox_table)).all() == []
    assert not (tmp_path / "journal.jsonl").exists()


@pytest.mark.parametrize("corrupt_state", [False, True])
def test_guard_error_rolls_back_before_open_connection_reuse(engine: Engine, corrupt_state: bool) -> None:
    error = AuditIntegrityError("clock failed")
    expected = TypeError if corrupt_state else AuditIntegrityError
    with engine.connect() as conn:
        with (
            patch("elspeth.core.landscape.lease_deadlines.read_landscape_decision_time", side_effect=error),
            pytest.raises(expected),
            conn.begin(),
        ):
            conn.execute(_PROBE.insert().values(id=1))
            record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=10)
            if corrupt_state:
                conn.info["elspeth_issued_lease_deadlines"] = "corrupted owned state"
        with conn.begin():
            conn.execute(_PROBE.insert().values(id=2))
    with engine.connect() as conn:
        assert list(conn.scalars(select(_PROBE.c.id).order_by(_PROBE.c.id))) == [2]


def test_failed_physical_rollback_invalidates_without_reconnecting(engine: Engine) -> None:
    connections: list[object] = []
    event.listen(engine, "connect", lambda dbapi, record: connections.append(dbapi))
    engine.dispose()
    with engine.connect() as conn:
        dbapi_proxy = conn.connection
        with (
            patch("elspeth.core.landscape.lease_deadlines.read_landscape_decision_time", return_value=_NOW),
            patch.object(dbapi_proxy, "rollback", side_effect=OSError("rollback failed")),
            pytest.raises(OSError, match="rollback failed"),
            conn.begin(),
        ):
            conn.execute(_PROBE.insert().values(id=1))
            record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=10)
        assert conn.invalidated
        assert len(connections) == 1
        with conn.begin():
            assert not has_issued_deadline(conn, key=_KEY)
            conn.execute(_PROBE.insert().values(id=2))
    assert len(connections) == 2
    with engine.connect() as conn:
        assert list(conn.scalars(select(_PROBE.c.id).order_by(_PROBE.c.id))) == [2]


def test_journal_error_before_guard_rolls_back_and_clears_open_connection(engine: Engine, tmp_path: Path) -> None:
    journal = LandscapeJournal(str(tmp_path / "journal.jsonl"), fail_on_error=True)
    journal.attach(engine)
    with engine.connect() as conn:
        with (
            patch.object(journal, "_serialize_record", side_effect=ValueError("serialization failed")),
            pytest.raises(ValueError, match="serialization failed"),
            conn.begin(),
        ):
            conn.execute(_PROBE.insert().values(id=1))
            record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=10)
        with conn.begin():
            assert not has_issued_deadline(conn, key=_KEY)
            assert conn.execute(select(_PROBE)).all() == []
            conn.execute(_PROBE.insert().values(id=2))
    with engine.connect() as conn:
        assert list(conn.scalars(select(_PROBE.c.id))) == [2]


@pytest.mark.parametrize("journal_enabled", [False, True])
def test_body_invalidation_preserves_error_without_cleanup_reconnect(engine: Engine, tmp_path: Path, journal_enabled: bool) -> None:
    if journal_enabled:
        LandscapeJournal(str(tmp_path / "journal.jsonl"), fail_on_error=True).attach(engine)
    connections: list[object] = []
    event.listen(engine, "connect", lambda dbapi, record: connections.append(dbapi))
    engine.dispose()
    with engine.connect() as conn:
        with pytest.raises(RuntimeError, match="original body failure"), conn.begin():
            record_issued_deadline(conn, key=_KEY, expires_at=_NOW, window_seconds=10)
            conn.invalidate()
            raise RuntimeError("original body failure")
        assert conn.invalidated
        assert len(connections) == 1
        with conn.begin():
            assert not has_issued_deadline(conn, key=_KEY)
    assert len(connections) == 2
