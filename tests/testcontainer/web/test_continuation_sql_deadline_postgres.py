"""Actual held-table read through the current required-SQL deadline owner."""

import asyncio
import threading
import time

import pytest
from psycopg2.errors import LockNotAvailable, QueryCanceled
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target as operation_postgres_target
from tests.testcontainer.web.test_composer_operations_postgres import operation_store as _operation_store
from tests.unit.web.coordination.test_composer_operation_authority import _wait_for_thread_event
from tests.unit.web.sessions.test_continuation_sql_deadline import _owners

from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator, RequiredWorkSource
from elspeth.web.sessions import locking

pytestmark = pytest.mark.testcontainer
operation_store = _operation_store


@pytest.mark.asyncio
async def test_real_get_for_turn_select_is_server_bounded_and_no_owner_keeps_existing_semantics(operation_store):
    engine = operation_store[0]
    assert engine.dialect.name == "postgresql" and engine.dialect.driver == "psycopg2"
    running, owner, _signal = _owners(operation_store)
    authority = operation_store[2]
    scope = RequiredWorkAuthority(
        RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context, running.claim.operation_id, running.claim.attempt
    )
    owned = RequiredWorkCoordinator(scope, sql_deadline_owner=owner)
    plain = RequiredWorkCoordinator(scope)
    entered = threading.Event()
    observations = []
    label = "positive"
    tasks = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if "FROM composer_async_operations" in statement:
            observations.append((label, locking._CONTINUATION_SQL_DEADLINE.get()))
            entered.set()

    async def read(work):
        ticket, projection = work.reserve_pair(
            RequiredWorkSource.CONTINUATION_READ_SQL,
            RequiredWorkSource.CONTINUATION_READ_PROJECTION,
            transition_ordinal=0,
            semantic_ordinal=0,
        )
        try:
            outcome = await run_required_sql_finish_once(
                ticket, authority.get_for_turn, session_id=running.claim.session_id, operation_id=running.claim.operation_id
            )
            work.validate_continuation_sql_outcome(
                ticket=ticket, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
            )
        except BaseException:
            projection.complete_without_submission()
            raise
        if type(outcome) is RequiredSQLReturned:
            projection.begin_projection()
            projection.complete_owned()
        else:
            projection.complete_without_submission()
        return outcome

    blocker = engine.connect()
    event.listen(engine, "before_cursor_execute", observe)
    try:
        positive = await read(owned)
        assert type(positive) is RequiredSQLReturned and positive.value is not None
        assert positive.value.operation_id == running.claim.operation_id
        assert observations == [("positive", (owner, False))]
        blocker.exec_driver_sql("LOCK TABLE composer_async_operations IN ACCESS EXCLUSIVE MODE")
        owner.begin_action(owner.policy.actions.read, started_at=time.monotonic())
        entered.clear()
        label = "bounded"
        bounded = asyncio.create_task(read(owned))
        tasks.append(bounded)
        await _wait_for_thread_event(entered)
        done, pending = await asyncio.wait((bounded,), timeout=owner.statement_cap + 2)
        assert done == {bounded} and not pending
        result = bounded.result()
        assert type(result) is RequiredSQLRaised and isinstance(result.error, OperationalError)
        assert isinstance(result.error.orig, (LockNotAvailable, QueryCanceled))
        assert result.error.orig.pgcode in ("57014", "55P03")
        assert observations[-1] == ("bounded", (owner, False))

        # The same real SELECT remains blocked without the optional V2 owner.
        # Observe it without cancelling the physically running worker; release
        # the owned blocker before joining its actual completion.
        entered.clear()
        label = "no_owner"
        ordinary = asyncio.create_task(read(plain))
        tasks.append(ordinary)
        await _wait_for_thread_event(entered)
        done, pending = await asyncio.wait((ordinary,), timeout=owner.statement_cap + 0.5)
        assert not done and pending == {ordinary}
        assert observations[-1] == ("no_owner", None)
        blocker.rollback()
        done, pending = await asyncio.wait((ordinary,), timeout=5)
        assert done == {ordinary} and not pending
        unblocked = ordinary.result()
        assert type(unblocked) is RequiredSQLReturned and unblocked.value is not None
        assert unblocked.value.operation_id == running.claim.operation_id
        assert owned.all_completed and plain.all_completed
    finally:
        blocker.rollback()
        blocker.close()
        try:
            if tasks:
                _done, pending = await asyncio.wait(tasks, timeout=5)
                assert not pending, "Actual SQL workers remain owned after releasing the test blocker"
        finally:
            event.remove(engine, "before_cursor_execute", observe)
            operation_store[1].release(running.session_operation_context)
