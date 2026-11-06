"""Actual continuation DATA reads retain WAL snapshots without owning writes."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident

import pytest
from sqlalchemy import Select, event
from sqlalchemy.sql.dml import Delete, Insert, Update

from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_caches import capture_continuation_caches, encode_continuation_caches
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    append_complete_snapshot,
    private_chunk_key,
    put_chunk,
    require_continuation_mutation,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from tests.unit.web.coordination import test_composer_operation_authority as fixtures
from tests.unit.web.sessions.test_continuation_runtime import runtime_case

operation_store = fixtures.operation_store


@pytest.mark.asyncio
async def test_runtime_read_finishes_before_actual_stop_writer_commits_and_fresh_actions_refuse(operation_store):
    engine, _repository, authority, _service, sid = operation_store
    arguments, _cache, _attachments = await runtime_case(operation_store)
    running = arguments["required_work"].running
    selected, inventory = arguments["selected"], arguments["inventory"]
    assert authority.read_continuation_runtime(running, selected=selected, inventory=inventory).selected is selected
    engine.dispose()
    entered, release = Event(), Event()
    writer_threads, reader_threads, statements, fresh_connections = [], [], [], []

    def connected(dbapi_connection, _record):
        fresh_connections.append(id(dbapi_connection))

    def trace(conn, _cursor, statement, _parameters, context, _many):
        statements.append((get_ident(), statement, context.invoked_statement, id(conn.connection.driver_connection)))

    def park_commit(_conn):
        if writer_threads and get_ident() == writer_threads[0]:
            entered.set()
            assert release.wait(45), "actual Stop writer was not released"

    def stop():
        writer_threads.append(get_ident())
        return authority.request_cancel(session_id=sid, operation_id=running.claim.operation_id)

    def read():
        reader_threads.append(get_ident())
        return authority.read_continuation_runtime(running, selected=selected, inventory=inventory)

    event.listen(engine, "before_cursor_execute", trace)
    event.listen(engine, "commit", park_commit)
    event.listen(engine, "connect", connected)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            writer = workers.submit(stop)
            try:
                assert entered.wait(10), "actual Stop did not reach its uncommitted transaction"
                reader = workers.submit(read)
                source = reader.result(timeout=20)
                assert source.selected is selected and not release.is_set() and not writer.done()
            finally:
                release.set()
                stopped = writer.result(timeout=10)
    finally:
        event.remove(engine, "commit", park_commit)
        event.remove(engine, "before_cursor_execute", trace)
        event.remove(engine, "connect", connected)
    assert stopped.cancel_requested_at is not None
    reader_sql = [row for row in statements if row[0] == reader_threads[0]]
    writer_sql = [row for row in statements if row[0] == writer_threads[0]]
    assert [row[1] for row in reader_sql if row[1] in ("BEGIN", "BEGIN IMMEDIATE")] == ["BEGIN"]
    assert any(row[1] == "BEGIN IMMEDIATE" for row in writer_sql)
    assert len({row[3] for row in reader_sql}) == 1
    assert {row[3] for row in reader_sql}.isdisjoint({row[3] for row in writer_sql})
    assert {row[3] for row in reader_sql}.issubset(fresh_connections)
    assert len(set(fresh_connections)) >= 2
    assert any(isinstance(row[2], Update) for row in writer_sql)
    assert not any(isinstance(row[2], (Delete, Insert, Update)) for row in reader_sql)
    with engine.connect() as conn:
        before = fixtures._continuation_guard_rows(conn)
    with pytest.raises(ComposerOperationCancelledDuringTurn):
        authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
    with pytest.raises(ComposerOperationCancelledDuringTurn):
        authority.prepare_provider_action(
            running,
            family=0,
            input_sha256="a" * 64,
            snapshot_json=encode_continuation_snapshot(selected.stack),
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
        )
    with engine.connect() as conn:
        assert fixtures._continuation_guard_rows(conn) == before
    arguments["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_runtime_read_keeps_complete_original_cohort_when_a_new_head_commits(operation_store):
    engine, _repository, authority, _service, sid = operation_store
    arguments, history, _attachments = await runtime_case(operation_store)
    running = arguments["required_work"].running
    selected, inventory = arguments["selected"], arguments["inventory"]
    source = authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
    cache = encode_continuation_caches(
        capture_continuation_caches(
            discovery={},
            preflight={},
            last_preflight=None,
            creator=running.session_operation_context,
            prior=history,
            env_ref_names=inventory.names,
        ),
        env_ref_names=inventory.names,
    )
    frames = tuple(
        frame.model_copy(update={"caches": cache.reference}) if type(frame) is ComposeFrame else frame for frame in selected.stack.frames
    )
    changed = selected.stack.model_copy(update={"frames": frames})
    assert changed != selected.stack and cache.reference not in tuple(chunk.reference for chunk in selected.chunks)
    entered, release = Event(), Event()
    reader_threads = []

    def park_after_actual_table_read(_conn, _cursor, _statement, _parameters, context, _many):
        statement = context.invoked_statement
        if (
            reader_threads
            and get_ident() == reader_threads[0]
            and not entered.is_set()
            and isinstance(statement, Select)
            and any(table is attempts for table in statement.get_final_froms())
        ):
            entered.set()
            assert release.wait(45), "actual snapshot reader was not released"

    def read():
        reader_threads.append(get_ident())
        return authority.read_continuation_runtime(running, selected=selected, inventory=inventory)

    def publish():
        with locked_session_transaction(engine, str(sid)) as conn:
            assert require_continuation_mutation(conn, running.session_operation_context)
            put_chunk(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                key=private_chunk_key(cache),
                payload=cache.canonical.encode("utf-8"),
                codec=cache.reference.codec,
            )
            return append_complete_snapshot(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.stack.cursor,
                snapshot_json=encode_continuation_snapshot(changed),
                event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"concurrent complete cache history"}'),),
                handoff=False,
            )

    event.listen(engine, "after_cursor_execute", park_after_actual_table_read)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            reader = workers.submit(read)
            try:
                assert entered.wait(20), "reader did not establish its actual table snapshot"
                writer = workers.submit(publish)
                committed = writer.result(timeout=20)
                assert not release.is_set() and not reader.done()
            finally:
                release.set()
            observed = reader.result(timeout=20)
    finally:
        event.remove(engine, "after_cursor_execute", park_after_actual_table_read)
    assert observed.selected is selected
    assert (observed.bootstrap, observed.original, observed.iteration, observed.evidence, observed.policy, observed.attachments) == (
        source.bootstrap,
        source.original,
        source.iteration,
        source.evidence,
        source.policy,
        source.attachments,
    )
    assert (observed.timing.sequence, observed.timing.generation, observed.timing.watermark_seconds) == (
        source.timing.sequence,
        source.timing.generation,
        source.timing.watermark_seconds,
    )
    assert observed.timing.observed_at >= source.timing.observed_at
    assert observed.selected.chunks == selected.chunks
    assert authority.load_continuation_snapshot(running) == committed
    with pytest.raises(ContinuationCASConflict):
        authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
    current = await load_selected_continuation(
        authority=authority, running=running, selected=committed, required_work=arguments["required_work"]
    )
    fresh_inventory = await materialize_continuation_env_inventory(
        advisor=arguments["service"]._advisor_checkpoint,
        authority=authority,
        selected=current,
        required_work=arguments["required_work"],
    )
    fresh = authority.read_continuation_runtime(running, selected=current, inventory=fresh_inventory)
    assert fresh.selected is current and cache in fresh.selected.chunks
    assert fresh.selected.stack != selected.stack
    arguments["required_work"].coordinator.assert_completed()
