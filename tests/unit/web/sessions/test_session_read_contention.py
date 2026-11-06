"""Ownership reads see committed rows while a real WAL writer is parked."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import update

from elspeth.web.sessions.models import sessions_table
from tests.unit.web.coordination import test_composer_operation_authority as operation_fixtures

operation_store = operation_fixtures.operation_store


@pytest.mark.parametrize("stream", [False, True], ids=["async-ownership", "stream-ownership"])
def test_ownership_read_returns_committed_session_while_writer_is_parked(operation_store, stream):
    engine, _repository, _authority, service, sid = operation_store
    original = service.get_session_for_stream(sid)
    entered, release = Event(), Event()

    def hold_writer():
        with engine.begin() as conn:
            assert (
                conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(title="Uncommitted title")).rowcount == 1
            )
            entered.set()
            assert release.wait(10), "ownership reader did not release its actual writer opponent"

    def read():
        return service.get_session_for_stream(sid) if stream else asyncio.run(service.get_session(sid))

    with ThreadPoolExecutor(max_workers=2) as workers:
        writer = workers.submit(hold_writer)
        try:
            assert entered.wait(5), "actual writer did not enter its transaction"
            reader = workers.submit(read)
            observed = reader.result(timeout=3)
            assert not release.is_set() and not writer.done()
            assert observed.id == original.id and observed.title == original.title
        finally:
            release.set()
            writer.result(timeout=5)

    assert service.get_session_for_stream(sid).title == "Uncommitted title"
