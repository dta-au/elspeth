"""Auth history must share the export snapshot across every keyset page."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.export_read_model import open_export_read_transaction
from elspeth.core.landscape.schema import auth_events_table

CUTOFF = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def sqlite_db(tmp_path: Path) -> Iterator[LandscapeDB]:
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'auth-export.db'}")
    try:
        yield db
    finally:
        db.close()


def insert_event(connection: Connection, event_id: str, *, occurred_at: datetime = CUTOFF, metadata_json: str = "{}") -> None:
    connection.execute(
        auth_events_table.insert().values(
            event_id=event_id,
            occurred_at=occurred_at,
            event_type="login",
            outcome="success",
            provider="vanguard",
            identity_id="identity-visible",
            metadata_json=metadata_json,
        )
    )


def assert_timestamp_pages(db: LandscapeDB, batch_size: int) -> None:
    with db.engine.begin() as writer:
        for event_id in ("e", "d", "c", "b", "a"):
            insert_event(writer, event_id)
        insert_event(writer, "z-earlier", occurred_at=CUTOFF - timedelta(seconds=1))
        insert_event(writer, "a-future", occurred_at=CUTOFF + timedelta(microseconds=1))
    with open_export_read_transaction(db.engine) as model:
        records = list(model.iter_auth_events(CUTOFF, batch_size=batch_size))
    assert [record["event_id"] for record in records] == ["z-earlier", "a", "b", "c", "d", "e"]
    assert records[-1]["occurred_at"] == "2026-09-10T12:00:00.000000Z"
    assert records[-1]["identity_id"] == "identity-visible"


def test_sqlite_snapshot_excludes_backdated_event_committed_between_pages(sqlite_db: LandscapeDB) -> None:
    with sqlite_db.engine.begin() as writer:
        insert_event(writer, "a")
        insert_event(writer, "c")
    with open_export_read_transaction(sqlite_db.engine) as model:
        records = model.iter_auth_events(CUTOFF, batch_size=1)
        assert next(records)["event_id"] == "a"
        with sqlite_db.engine.begin() as writer:
            assert writer.connection.driver_connection is not model.connection.connection.driver_connection
            insert_event(writer, "b")
            insert_event(writer, "older-late", occurred_at=CUTOFF - timedelta(seconds=1))
        assert [record["event_id"] for record in records] == ["c"]
        assert [record["event_id"] for record in model.iter_auth_events(CUTOFF, batch_size=1)] == ["a", "c"]
    # Positive control: both committed rows really exist and a fresh snapshot sees them.
    with open_export_read_transaction(sqlite_db.engine) as fresh:
        assert [record["event_id"] for record in fresh.iter_auth_events(CUTOFF, batch_size=1)] == ["older-late", "a", "b", "c"]


@pytest.mark.parametrize("batch_size", [1, 2, 3, 6, 10])
def test_sqlite_equal_timestamps_cross_page_boundaries(sqlite_db: LandscapeDB, batch_size: int) -> None:
    assert_timestamp_pages(sqlite_db, batch_size)


@pytest.mark.parametrize("metadata_json", ["broken-json", "[]", "null", '"scalar"'])
def test_sqlite_corrupt_auth_metadata_fails_after_valid_page(sqlite_db: LandscapeDB, metadata_json: str) -> None:
    with sqlite_db.engine.begin() as writer:
        insert_event(writer, "a", metadata_json='{"safe": true}')
        insert_event(writer, "b", metadata_json=metadata_json)
    with open_export_read_transaction(sqlite_db.engine) as model:
        assert model.connection.scalar(select(auth_events_table.c.event_id).where(auth_events_table.c.event_id == "b")) == "b"
        records = model.iter_auth_events(CUTOFF, batch_size=1)
        assert next(records)["metadata"] == {"safe": True}
        with pytest.raises(AuditIntegrityError, match="Auth event b metadata"):
            list(records)
