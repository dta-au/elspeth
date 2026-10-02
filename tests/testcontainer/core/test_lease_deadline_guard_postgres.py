"""Real database-clock and journal-tail completion proofs on PostgreSQL."""

from collections.abc import Iterator
from datetime import UTC, timedelta
from pathlib import Path
from time import sleep
from unittest.mock import patch

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select, text
from sqlalchemy.engine import Engine
from tests.helpers.postgres_target import postgres_test_target

from elspeth.core.landscape.database_clock import read_landscape_decision_time, read_landscape_transaction_time
from elspeth.core.landscape.journal import JournalRecord, LandscapeJournal
from elspeth.core.landscape.lease_deadlines import (
    DeadlineKey,
    DeadlineKind,
    LeaseDeadlineExpiredError,
    has_issued_deadline,
    install_deadline_guard,
    record_issued_deadline,
)
from elspeth.core.landscape.schema import sidecar_journal_outbox_table

pytestmark = pytest.mark.testcontainer
_PROBE = Table("deadline_guard_probe", MetaData(), Column("id", Integer, primary_key=True))
_KEY = DeadlineKey(DeadlineKind.SINK_EFFECT, ("effect",))


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


@pytest.fixture
def engine(postgres_url: str) -> Iterator[Engine]:
    engine = create_engine(postgres_url, pool_size=1, max_overflow=0)
    _PROBE.create(engine, checkfirst=True)
    sidecar_journal_outbox_table.create(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(_PROBE.delete())
        conn.execute(sidecar_journal_outbox_table.delete())
    try:
        yield engine
    finally:
        engine.dispose()


def test_fresh_clock_advances_within_transaction_and_normalizes_non_utc_session(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL TIME ZONE 'Australia/Canberra'"))
        started = read_landscape_transaction_time(conn)
        before = read_landscape_decision_time(conn)
        conn.execute(text("SELECT pg_sleep(0.03)"))
        after = read_landscape_decision_time(conn)
        assert read_landscape_transaction_time(conn) == started
        assert before >= started
        assert after - before >= timedelta(milliseconds=25)
        assert after.tzinfo is UTC


@pytest.mark.parametrize("attach_first", [False, True])
def test_delayed_journal_refuses_and_rolls_back_before_same_connection_reuse(engine: Engine, tmp_path: Path, attach_first: bool) -> None:
    journal_path = tmp_path / "guard.jsonl"
    journal = LandscapeJournal(str(journal_path), fail_on_error=True)
    if attach_first:
        journal.attach(engine)
        install_deadline_guard(engine)
    else:
        install_deadline_guard(engine)
        journal.attach(engine)
    original = journal._serialize_record
    serialized: list[JournalRecord] = []

    def delayed_serialization(record: JournalRecord) -> str:
        serialized.append(record)
        sleep(0.15)
        return original(record)

    with engine.connect() as conn:
        with (
            patch.object(journal, "_serialize_record", side_effect=delayed_serialization),
            pytest.raises(LeaseDeadlineExpiredError),
            conn.begin(),
        ):
            conn.execute(_PROBE.insert().values(id=1))
            now = read_landscape_decision_time(conn)
            record_issued_deadline(conn, key=_KEY, expires_at=now + timedelta(seconds=0.1), window_seconds=0.1)
        assert len(serialized) == 1
        assert not journal_path.exists()
        with conn.begin():
            assert not has_issued_deadline(conn, key=_KEY)
            assert conn.execute(select(_PROBE)).all() == []
            assert conn.execute(select(sidecar_journal_outbox_table)).all() == []
            conn.execute(_PROBE.insert().values(id=2))
    with engine.connect() as conn:
        assert list(conn.scalars(select(_PROBE.c.id))) == [2]
        assert conn.execute(select(sidecar_journal_outbox_table)).all() == []
    # Only the later successful transaction may reach the journal file.
    assert '"parameters": [1]' not in journal_path.read_text()
    assert journal_path.read_text().count('"journal_batch_ordinal"') == 1
