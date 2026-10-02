"""Two real PostgreSQL backends prove snapshot-bound auth-event enumeration."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from tests.helpers.postgres_target import postgres_test_target
from tests.unit.core.landscape.test_auth_event_export_snapshot import CUTOFF, assert_timestamp_pages, insert_event

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.export_read_model import open_export_read_transaction

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def postgres_db() -> Iterator[LandscapeDB]:
    with postgres_test_target(driver="psycopg") as url:
        db = LandscapeDB(url)
        try:
            yield db
        finally:
            db.close()


def test_postgres_snapshot_excludes_backdated_transaction_committed_after_snapshot(postgres_db: LandscapeDB) -> None:
    with postgres_db.engine.begin() as writer:
        insert_event(writer, "a")
        insert_event(writer, "c")
    with postgres_db.engine.connect() as writer:
        transaction = writer.begin()
        insert_event(writer, "older-late", occurred_at=CUTOFF - timedelta(seconds=1))
        insert_event(writer, "b")
        with open_export_read_transaction(postgres_db.engine) as model:
            assert model.connection.exec_driver_sql("SHOW transaction_isolation").scalar_one() == "repeatable read"
            reader_pid = model.connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            assert reader_pid != writer.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            records = model.iter_auth_events(CUTOFF, batch_size=1)
            assert next(records)["event_id"] == "a"
            transaction.commit()
            assert [record["event_id"] for record in records] == ["c"]
            assert [record["event_id"] for record in model.iter_auth_events(CUTOFF, batch_size=1)] == ["a", "c"]
    with open_export_read_transaction(postgres_db.engine) as fresh:
        assert [record["event_id"] for record in fresh.iter_auth_events(CUTOFF, batch_size=1)] == ["older-late", "a", "b", "c"]


@pytest.mark.parametrize("batch_size", [1, 2, 3, 6, 10])
def test_postgres_equal_timestamps_cross_page_boundaries(postgres_db: LandscapeDB, batch_size: int) -> None:
    assert_timestamp_pages(postgres_db, batch_size)


@pytest.mark.parametrize("metadata_json", ["broken-json", "[]", "null", '"scalar"'])
def test_postgres_corrupt_auth_metadata_fails_after_valid_page(postgres_db: LandscapeDB, metadata_json: str) -> None:
    with postgres_db.engine.begin() as writer:
        insert_event(writer, "a", metadata_json='{"safe": true}')
        insert_event(writer, "b", metadata_json=metadata_json)
    with open_export_read_transaction(postgres_db.engine) as model:
        records = model.iter_auth_events(CUTOFF, batch_size=1)
        assert next(records)["metadata"] == {"safe": True}
        with pytest.raises(AuditIntegrityError, match="Auth event b metadata"):
            list(records)
