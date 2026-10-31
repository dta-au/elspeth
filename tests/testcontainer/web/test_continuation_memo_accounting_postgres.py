"""Direct psycopg3 structure controls and bounded private-reader prerequisites.

The standalone return calibration intentionally owns its actual Row operands
through the assertions. It is not a retention observer or a lifetime proof for
the full private loader. The observed reader control must qualify independently.
"""

from __future__ import annotations

import ctypes
import dis
import sys
import threading
from hashlib import sha256
from types import FunctionType, MappingProxyType

import psycopg
import pytest
from sqlalchemy import event
from sqlalchemy.dialects.postgresql.base import PGCompiler
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg, PGExecutionContext_psycopg
from sqlalchemy.engine import Connection, CursorResult, Result, Row
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.selectable import Select
from tests.helpers import continuation_memo as helper
from tests.helpers.continuation_memo import RetentionMemoTrace
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.helpers.postgres_target import postgres_test_target
from tests.testcontainer.web import test_composer_operations_postgres as operations_pg
from tests.unit.web import test_continuation_memo_accounting as accounting
from tests.unit.web.sessions.test_continuation_cold_restore import admit_continuation, start_continuation

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import async_workers
from elspeth.web.composer import continuation_clock as clock
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_codec import encode_private_chunk
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredSQLRaised,
    RequiredSQLReturned,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkSource,
)
from elspeth.web.sessions import continuation as store
from elspeth.web.sessions.locking import locked_session_transaction

pytestmark = pytest.mark.testcontainer
operation_store = operations_pg.operation_store


@pytest.fixture(scope="module")
def operation_postgres_target():
    with postgres_test_target(driver="psycopg", mem_limit="256m", nano_cpus=500_000_000) as target:
        yield target


@pytest.fixture
def pg_continuation(operation_store):
    engine, repository, authority, _service, _sid = operation_store
    assert type(engine.dialect) is PGDialect_psycopg and engine.dialect.dbapi is psycopg
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    context = running.session_operation_context
    try:
        assert running.claim.operation_id == record.operation_id and running.claim.attempt == 1
        yield engine, context, record.operation_id
    finally:
        repository.release(context)


@pytest.fixture
def pg_private_chunk(pg_continuation):
    engine, context, operation_id = pg_continuation
    payload = (
        b'{"current_indexes":[0],"messages_json":["{\\"content\\":\\"caf\xc3\xa9 and a bounded private prefix\\",\\"role\\":\\"user\\"}"]}'
    )
    private = encode_private_chunk(codec="composer.messages.v1", canonical=payload.decode("utf-8"))
    reference = private.reference
    assert reference.byte_length == len(payload) and reference.digest == sha256(payload).hexdigest()
    with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
        assert store.require_continuation_mutation(conn, context) is True
        assert (
            store.put_chunk(
                conn,
                session_id=str(context.fence.session_id),
                operation_id=operation_id,
                key=f"value:{reference.codec}:{reference.digest}",
                payload=payload,
                codec=reference.codec,
            )
            is None
        )
    return engine, context, operation_id, reference, payload


def _pg_put_twice(engine, context, operation_id, key, payload):
    with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
        assert store.require_continuation_mutation(conn, context) is True
        public = 0
        for _ in range(2):
            assert (
                store.put_chunk(
                    conn,
                    session_id=str(context.fence.session_id),
                    operation_id=operation_id,
                    key=key,
                    payload=payload,
                    codec="control.v1",
                )
                is None
            )
            public += 1
        assert store.read_chunk(conn, session_id=str(context.fence.session_id), operation_id=operation_id, key=key) == payload
    return public


@pytest.mark.asyncio
async def test_pg_structure_original_put_chunk(monkeypatch, request, record_property, pg_continuation):
    engine, context, operation_id = pg_continuation
    payload = b"independently authored PG structure chunk"
    native = []

    def after(_conn, _cursor, _statement, parameters, current, many):
        if type(current.invoked_statement) is Insert:
            assert current.invoked_statement.table is store.chunks and not many
            assert type(parameters) is dict and len(parameters) == 7
            native.append(7)

    def work():
        assert _pg_put_twice(engine, context, operation_id, "pg-original-row", payload) == 2
        assert native == [7]
        record_property("pg_native_public_readback", (1, 2, 1))
        return len(payload)

    result, snapshot = await accounting._closed_dml_trial(
        monkeypatch, request, engine, context, operation_id, work, listeners=((engine, "after_cursor_execute", after, False),)
    )
    assert result == len(payload)
    (row,) = accounting._closed_dml_rows(snapshot)
    assert row.sql.kind == "insert" and row.sql.bind_arities == (7,) and not row.sql.executemany and row.outcome == "returned"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_scope", ["slot", "container"])
async def test_pg_structure_callback_row_copy(monkeypatch, request, record_property, pg_continuation, replacement_scope):
    engine, context, operation_id = pg_continuation
    payload = b"independently authored PG callback row"
    replacements, native = [], []

    def before(_conn, _cursor, statement, parameters, current, many):
        if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
            assert not replacements and not many and type(parameters) is dict and len(parameters) == 7
            assert type(current.parameters) is list and len(current.parameters) == 1 and current.parameters[0] is parameters
            original_container = id(current.parameters)
            replacement = dict(parameters)
            assert replacement is not parameters and replacement.keys() == parameters.keys()
            assert all(replacement[name] is value for name, value in parameters.items())
            if replacement_scope == "slot":
                current.parameters[0] = replacement
                assert id(current.parameters) == original_container
            else:
                current.parameters = [replacement]
                assert id(current.parameters) != original_container
            replacements.append((id(parameters), id(replacement), original_container, id(current.parameters)))
            return statement, replacement
        return statement, parameters

    def after(_conn, _cursor, _statement, parameters, current, many):
        if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
            assert len(replacements) == 1 and type(parameters) is dict and len(parameters) == 7 and not many
            assert id(parameters) == replacements[0][1] and id(current.parameters) == replacements[0][3]
            assert current.parameters[0] is parameters
            native.append(7)

    def work():
        assert _pg_put_twice(engine, context, operation_id, "pg-copied-callback-row", payload) == 2
        assert native == [7] and len(replacements) == 1 and replacements[0][0] != replacements[0][1]
        record_property("pg_row_copy_native_public_readback", (1, 2, 1))
        record_property("pg_row_copy_reached", (1, int(replacement_scope == "container")))
        return len(payload)

    result, snapshot = await accounting._closed_dml_trial(
        monkeypatch,
        request,
        engine,
        context,
        operation_id,
        work,
        refused=True,
        listeners=((engine, "before_cursor_execute", before, True), (engine, "after_cursor_execute", after, False)),
    )
    assert result == len(payload)
    (row,) = accounting._closed_dml_rows(snapshot)
    assert row.sql.bind_arities == (7,) and row.outcome == "returned"
    assert "publication_row_callback_return_changed" in snapshot.errors


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [False, True])
async def test_pg_structure_source_slot_interval(monkeypatch, request, record_property, pg_continuation, change):
    guards = helper._STRUCTURE_STORE_VISITOR_GUARDS
    matches = [(index, fact) for index, fact in enumerate(guards) if fact[0] is PGCompiler and fact[1] == "visit_max_func"]
    assert len(matches) == 1
    index, (owner, name, present, function, code) = matches[0]
    assert index > 0 and owner is PGCompiler and name == "visit_max_func"
    assert present is False and function is None and code is None and "visit_max_func" not in PGCompiler.__dict__
    earlier_owner, earlier_name, earlier_present, earlier_function, earlier_code = guards[0]
    assert (earlier_name in earlier_owner.__dict__) is earlier_present
    assert earlier_owner.__dict__.get(earlier_name) is earlier_function
    assert not earlier_present or (type(earlier_function) is FunctionType and earlier_function.__code__ is earlier_code)
    record_property("pg_late_store_visitor_guard", (index, 0, 0, 0))
    engine, context, operation_id = pg_continuation
    prior, monitoring = sys.getprofile(), accounting._native_monitor_state()
    payload = b"independently authored PG source slot interval"
    intervals, native = [], []

    def before(_conn, _cursor, statement, parameters, current, many):
        if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
            assert not intervals and type(parameters) is dict and len(parameters) == 7 and not many
            assert "visit_max_func" not in PGCompiler.__dict__
            hook = sys.getprofile()
            assert type(hook) is FunctionType and hook.__code__ is helper._LIFETIME_PROFILE_CODE
            trace = hook.__closure__[helper._LIFETIME_PROFILE_CELL_INDEXES[1]].cell_contents
            assert type(trace) is RetentionMemoTrace
            thread = trace._structure_threads[threading.get_ident()]
            count, start = thread.error_count, len(thread.errors)
            # Preserve the first existing refusal before this control can
            # plant a slot. A capped prefix is not a complete error inventory.
            record_property("pg_late_slot_prior_refusal", (count, start, tuple(thread.errors)))
            record_property("pg_late_slot_internal_errors", tuple(thread.internal_errors))
            assert count == start == 0, "the changed slot must be the first refusal"
            if change:
                PGCompiler.visit_max_func = None
            try:
                arity = len(parameters)
            finally:
                if change:
                    del PGCompiler.visit_max_func
            errors = thread.errors[start:]
            intervals.append((int(change), arity, thread.error_count - count, errors.count("original_source_changed")))
        return statement, parameters

    def after(_conn, _cursor, _statement, parameters, current, many):
        if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
            assert "visit_max_func" not in PGCompiler.__dict__
            assert len(intervals) == 1 and intervals[0][:2] == (int(change), 7)
            assert type(parameters) is dict and len(parameters) == 7 and not many
            native.append(7)

    def work():
        assert _pg_put_twice(engine, context, operation_id, "pg-late-absent-source-slot", payload) == 2
        assert native == [7] and len(intervals) == 1
        record_property("pg_late_slot_interval", tuple(intervals))
        record_property("pg_late_slot_native_public_readback", (1, 2, 1))
        return len(payload)

    try:
        result, snapshot = await accounting._closed_dml_trial(
            monkeypatch,
            request,
            engine,
            context,
            operation_id,
            work,
            refused=change,
            listeners=((engine, "before_cursor_execute", before, True), (engine, "after_cursor_execute", after, False)),
        )
        assert result == len(payload) and intervals == [(int(change), 7, int(change), int(change))]
        assert helper._STRUCTURE_STORE_VISITOR_GUARDS is guards
        (row,) = accounting._closed_dml_rows(snapshot)
        assert row.sql.bind_arities == (7,) and row.outcome == "returned"
        assert snapshot.errors == (("original_source_changed",) if change else ())
    finally:
        assert "visit_max_func" not in PGCompiler.__dict__
        assert sys.getprofile() is prior and accounting._native_monitor_state() == monitoring


def _private_projection(current):
    statement = current.invoked_statement
    return type(statement) is Select and tuple(statement.selected_columns.keys()) == (
        "codec",
        "sha256",
        "byte_length",
        "actual_byte_length",
        "payload",
    )


@pytest.mark.asyncio
async def test_pg_private_original_bounded_reader_structure(monkeypatch, request, record_property, pg_private_chunk):
    """The closed observer must accept this actual projection before Row claims."""
    engine, context, operation_id, reference, payload = pg_private_chunk
    prior = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request)
    native = []

    def after(_conn, cursor, _statement, parameters, current, many):
        if _private_projection(current):
            assert type(cursor) is psycopg.Cursor and type(parameters) is dict and not many
            assert len(parameters) == 4 and len(cursor.description) == 5
            native.append((4, 5))

    try:
        trace.observe_structure(engines=(engine,), entrypoint="calibration")
        handles = trace.structure_listener_handles()
        coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, operation_id, 1))
        ticket = coordinator.reserve(RequiredWorkSource.CONTINUATION_READ_SQL)
        try:
            event.listen(engine, "after_cursor_execute", after)

            def work():
                with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
                    assert store.require_continuation_mutation(conn, context) is True
                    for _ in range(2):
                        actual = store.read_private_chunk(
                            conn, session_id=str(context.fence.session_id), operation_id=operation_id, reference=reference
                        )
                        assert type(actual) is bytes and actual == payload
                assert native == [(4, 5), (4, 5)]
                record_property("pg_private_native_public", (2, 4, 5, 2))
                return len(payload)

            async def operation():
                outcome = await async_workers.run_required_sql_finish_once(ticket, work)
                coordinator.validate_continuation_sql_outcome(
                    ticket=ticket, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
                )
                if isinstance(outcome, RequiredSQLRaised):
                    raise outcome.error
                assert isinstance(outcome, RequiredSQLReturned)
                assert ticket.complete and not ticket.errors and async_workers.outstanding_admissions() == 0
                return outcome.value

            timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
            timer.finish_preparation()
            assert await timer.run("retention", operation) == len(payload)
        finally:
            primary = sys.exception()
            try:
                if event.contains(engine, "after_cursor_execute", after):
                    event.remove(engine, "after_cursor_execute", after)
            except BaseException as cleanup:
                trace._observation_incomplete = True
                trace.errors.append("PG reader listener cleanup interrupted")
                if primary is None:
                    raise
                primary.add_note("PG reader listener cleanup: " + type(cleanup).__name__)
        coordinator.assert_completed()
        # Real native/reader premises precede this deliberately strict gate.
        trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        snapshot = trace.structure_snapshot()
        assert snapshot.required and snapshot.closed and snapshot.complete and snapshot.observation_verified and not snapshot.errors
        assert all(not event.contains(target, name, callback) for target, name, callback in handles)
        assert sys.getprofile() is prior
    except BaseException as primary:
        trace.close(primary=primary)
        raise


def _pg_unwind_sites(reader):
    """Immutable grammar of this held reader, never an observed-offset cache."""
    assert reader.__code__ is helper._READ_PRIVATE_CHUNK_CODE
    # 3.13 omits CACHE Instructions even with show_caches=True. Both supported
    # interpreters expose original two-byte cache words in the held co_code.
    instructions = tuple(dis.get_instructions(reader.__code__))
    loads = [item for item in instructions if item.opname == "LOAD_ATTR" and item.argval == "execute"]
    assert len(loads) == 1
    (load,) = loads
    calls = [
        item
        for item in instructions
        if item.offset > load.offset
        and item.opname == "CALL"
        and item.positions.lineno == load.positions.lineno
        and item.positions.col_offset == load.positions.col_offset
    ]
    assert calls and calls[0].arg == 1
    execute = calls[0]
    earlier = [item for item in instructions if item.offset < execute.offset and item.opname == "CALL"]
    assert earlier and earlier[-1].arg == 3
    where = earlier[-1]
    where_loads = [item for item in instructions if item.opname == "LOAD_ATTR" and item.argval == "where" and item.offset < where.offset]
    assert len(where_loads) == 1
    assert where.positions.lineno == where_loads[0].positions.lineno
    assert where.positions.col_offset == where_loads[0].positions.col_offset
    between = tuple(range(where.offset + 2, execute.offset, 2))
    assert len(between) == 3 and all(reader.__code__.co_code[offset] == dis.opmap["CACHE"] for offset in between)
    later = tuple(item for item in instructions if item.offset > execute.offset)
    assert later and later[0].opname == "LOAD_ATTR" and later[0].argval == "one_or_none"
    caches = tuple(range(execute.offset + 2, later[0].offset, 2))
    assert len(caches) == 3 and all(reader.__code__.co_code[offset] == dis.opmap["CACHE"] for offset in caches)
    result_calls = [item for item in later if item.opname == "CALL"]
    assert result_calls and result_calls[0].arg == 0
    tail = tuple(item for item in later if item.offset > result_calls[0].offset and item.opname != "CACHE")
    assert tail and tail[0].opname == "STORE_FAST" and tail[0].argval == "row"
    row_test = tail[1:4]
    assert len(row_test) == 3
    assert row_test[0].opname == "LOAD_FAST" and row_test[0].argval == "row"
    assert row_test[1].opname == "EXTENDED_ARG" and row_test[1].arg == 1
    assert row_test[2].opname == "POP_JUMP_IF_NONE"
    assert row_test[2].positions.lineno == row_test[0].positions.lineno
    assert row_test[2].positions.col_offset == row_test[0].positions.col_offset
    direct_index = next(index for index, item in enumerate(instructions) if item.offset == row_test[2].argval)
    direct = instructions[direct_index : direct_index + 4]
    assert tuple(item.opname for item in direct) == ("LOAD_GLOBAL", "LOAD_CONST", "CALL", "RAISE_VARARGS")
    assert direct[0].argval == "AuditIntegrityError"
    assert direct[1].argval == "private chunk complete byte bound or SQL binding mismatch"
    assert direct[2].arg == direct[3].arg == 1
    assert all(item.positions.lineno == direct[0].positions.lineno for item in direct)
    assert direct[0].positions.lineno > row_test[0].positions.lineno

    # 3.12 duplicates small exit blocks after chained-comparison cleanup. Bind
    # every original raise block, while the actual missing-row branch above
    # selects its own exact block rather than an ordinal or global count.
    raise_messages = []
    for index, item in enumerate(instructions):
        if item.opname != "RAISE_VARARGS":
            continue
        block = instructions[index - 3 : index + 1]
        assert tuple(part.opname for part in block) == ("LOAD_GLOBAL", "LOAD_CONST", "CALL", "RAISE_VARARGS")
        assert block[0].argval == "AuditIntegrityError" and block[2].arg == item.arg == 1
        assert all(part.positions.lineno == item.positions.lineno for part in block)
        raise_messages.append(block[1].argval)
    bound_error = "private chunk exceeds its complete document bound"
    dialect_error = "private chunk byte projection has an unsupported database"
    row_error = "private chunk complete byte bound or SQL binding mismatch"
    if sys.version_info[:2] == (3, 13):
        assert raise_messages == [bound_error, dialect_error, row_error]
        pair = execute.offset, between[-1]
    else:
        assert sys.version_info[:2] == (3, 12), "unproved native unwind interpreter"
        assert raise_messages == [bound_error, bound_error, dialect_error, row_error, row_error]
        pair = caches[-1], caches[-1]
    return pair, direct[3].offset, {"call-start": execute.offset, "prior-cache": between[-2], "execute-cache": caches[0]}


def _pg_require_unwind_site(frame, candidate_frame, code, candidate_offset, expected_frame, expected_offset):
    # Explicit raises keep the owned refusal literal intact under pytest's
    # assertion rewriting; the candidate predicates and their order stay exact.
    if not (candidate_frame is frame and frame.f_code is code):
        raise AssertionError("native unwind candidate frame changed")
    if not (frame.f_lasti == expected_frame and candidate_offset == expected_offset):
        raise AssertionError("native unwind candidate offset changed")


@pytest.mark.parametrize(
    ("replacement", "marker_type", "opposition"),
    [
        ("original", None, "original"),
        ("row", None, "original"),
        ("data", None, "original"),
        ("payload", None, "original"),
        ("callback", None, "original"),
        ("unwind", None, "original"),
        ("raised", RuntimeError, "original"),
        ("raised", KeyboardInterrupt, "original"),
        ("raised", SystemExit, "original"),
        ("raised", RuntimeError, "call-start"),
        ("raised", RuntimeError, "prior-cache"),
        ("raised", RuntimeError, "execute-cache"),
        ("raised", RuntimeError, "frame"),
    ],
)
def test_pg_private_native_return_calibration(record_property, pg_private_chunk, replacement, marker_type, opposition):
    """Calibrate real delivery and genuine substitutions without an observer."""
    engine, context, operation_id, reference, payload = pg_private_chunk
    reader = store.read_private_chunk
    only_one, one_or_none, fetch_one = Result._only_one_row, Result.one_or_none, CursorResult._fetchone_impl
    functions = (fetch_one, only_one, one_or_none, reader)
    codes = tuple(function.__code__ for function in functions)
    propagated_pair, direct_raise, wrong_offsets = _pg_unwind_sites(reader)
    execute = Connection.execute
    execute_code, execute_globals = execute.__code__, execute.__globals__
    assert all(type(function) is FunctionType for function in functions)
    prior = sys.getprofile()
    assert prior is None, "calibration requires the original unprofiled thread"
    assert store.read_private_chunk is reader and reader.__globals__ is store.__dict__
    locals_to_fast = ctypes.PYFUNCTYPE(None, ctypes.py_object, ctypes.c_int)(("PyFrame_LocalsToFast", ctypes.pythonapi))
    fast_to_locals = ctypes.PYFUNCTYPE(ctypes.c_int, ctypes.py_object)(("PyFrame_FastToLocalsWithError", ctypes.pythonapi))
    returned, unwound, mutations, byte_calls, callbacks, invalid_unwind = [], [], [], [], [], []
    unwind_attempts, unwind_errors = [], []
    candidates, opposed = [], []
    # These are TEST-owned strong operands, retained until every identity
    # assertion below. They must never become an observer's numeric custody.
    held, rows, results = [], [], []
    active = False
    marker = None if marker_type is None else marker_type("actual PG result callback primary")
    session_id = str(context.fence.session_id)
    identity = threading.get_ident()
    current_connection = None
    current_reference = reference

    def sources_current():
        assert Result._only_one_row is only_one and Result.one_or_none is one_or_none
        assert CursorResult._fetchone_impl is fetch_one and store.read_private_chunk is reader
        assert all(function.__code__ is code for function, code in zip(functions, codes, strict=True))
        assert store.sha256 is sha256
        assert Connection.execute is execute and execute.__code__ is execute_code and execute.__globals__ is execute_globals

    def reader_parent(frame, code):
        # Result methods serve many genuine setup/authority queries too. Only
        # this finite original reader chain belongs to the calibration.
        if code is codes[3]:
            return frame
        parent = frame.f_back
        if code is codes[0]:
            if parent is None or parent.f_code is not codes[1]:
                return None
            parent = parent.f_back
        if code is codes[0] or code is codes[1]:
            if parent is None or parent.f_code is not codes[2]:
                return None
            parent = parent.f_back
        return parent if parent is not None and parent.f_code is codes[3] else None

    def bind_reader(frame):
        assert threading.get_ident() == identity and frame.f_code is codes[3] and frame.f_globals is store.__dict__
        values = frame.f_locals
        assert current_connection is not None and values["conn"] is current_connection
        assert values["session_id"] is session_id and values["operation_id"] is operation_id
        assert values["reference"] is current_reference and type(current_reference) is PrivateChunkRef

    def bind_result(result, row=None):
        assert results and result is results[-1][0] and type(result) is CursorResult
        _result, _context, _cursor, _present, metadata, keymap = results[-1]
        if row is not None:
            assert type(row) is Row and row._parent is metadata and row._key_to_index is keymap

    def copy_row(row):
        assert active and replacement == "callback" and type(row) is Row
        caller = helper._LIFETIME_FRAME(1)
        assert caller.f_code is codes[1] and caller.f_globals is only_one.__globals__
        parent = reader_parent(caller, codes[1])
        assert parent is not None
        bind_reader(parent)
        bind_result(caller.f_locals["self"], row)
        parent, data, keymap = row._parent, row._data, row._key_to_index
        copied = Row(parent, None, keymap, data)
        assert type(copied) is Row and copied is not row and copied._parent is parent
        assert copied._data == data and all(a is b for a, b in zip(copied._data, data, strict=True))
        held.append((row, parent, data, data[4], copied, copied._data))
        callbacks.append(1)
        return copied

    def after_execute(_conn, _clause, _multiparams, _params, _options, result):
        if active and type(result) is CursorResult and _private_projection(result.context):
            caller = helper._LIFETIME_FRAME(1)
            # SQLAlchemy dispatches this original event below Connection.execute;
            # the live reader is still its ancestor, not a fabricated result.
            for _ in range(8):
                if caller is None or caller.f_code is codes[3]:
                    break
                caller = caller.f_back
            assert caller is not None and caller.f_code is codes[3], "the exact reader must own the finite event ancestry"
            bind_reader(caller)
            assert result._post_creational_filter is None
            assert type(result.cursor) is psycopg.Cursor and len(result.cursor.description) == 5
            assert result.connection is _conn is current_connection
            assert type(result.context) is PGExecutionContext_psycopg and result.context.root_connection is _conn
            assert result.context.cursor is result.cursor and result.context.dialect is engine.dialect
            present = "_post_creational_filter" in result.__dict__
            results.append((result, result.context, result.cursor, present, result._metadata, result._metadata._key_to_index))
            if replacement == "callback":
                result._post_creational_filter = copy_row
            elif replacement == "raised":
                assert marker is not None
                raise marker

    def profile(frame, kind, arg):
        if not active or frame.f_code is not codes[3] or kind != "c_call":
            return
        sources_current()
        bind_reader(frame)
        if arg is sha256:
            byte_calls.append("sha256")
        if arg is not len:
            return
        byte_calls.append("len")
        row = frame.f_locals["row"]
        assert type(row) is Row and len(row._data) == 5 and type(row.payload) is bytes
        bind_result(results[-1][0], row)
        if replacement not in ("row", "data", "payload") or mutations:
            return
        parent, data, keymap, original_payload = row._parent, row._data, row._key_to_index, row.payload
        if replacement == "row":
            copied = Row(parent, None, keymap, data)
            assert type(copied) is Row and copied is not row and copied._parent is parent
            frame.f_locals["row"] = copied
            locals_to_fast(frame, 0)
            assert fast_to_locals(frame) == 0 and frame.f_locals["row"] is copied
        else:
            copied = row
            copied_payload = bytes(bytearray(original_payload)) if replacement == "payload" else original_payload
            assert copied_payload == original_payload
            assert (copied_payload is not original_payload) is (replacement == "payload")
            new_data = tuple(value if index != 4 else copied_payload for index, value in enumerate(data))
            assert type(new_data) is tuple and new_data is not data
            row.__setstate__({"_parent": parent, "_data": new_data})
            assert row._parent is parent and row._data is new_data
            assert frame.f_locals["row"] is row
        assert copied._data == data and copied.payload == payload
        assert all(a is b for a, b in zip(copied._data[:4], data[:4], strict=True))
        held.append((row, parent, data, original_payload, copied, copied._data))
        mutations.append(replacement)

    def deliver(code, offset, value):
        if not any(code is known for known in codes):
            return
        frame = helper._LIFETIME_FRAME(1)
        assert active and frame.f_code is code, "native delivery must enter from the actual held producer frame"
        parent = reader_parent(frame, code)
        if parent is None:
            return
        sources_current()
        bind_reader(parent)
        index = next(index for index, known in enumerate(codes) if code is known)
        assert frame.f_globals is functions[index].__globals__
        assert frame.f_lasti == offset
        if code is codes[0]:
            bind_result(frame.f_locals["self"])
            if value is not None:
                assert type(value) is tuple and len(value) == 5
                held.append((value,))
        elif code is codes[1] or code is codes[2]:
            bind_result(frame.f_locals["self"], value)
            if value is not None:
                assert type(value) is Row and len(value._data) == 5
                held.append((value, value._parent, value._data, value.payload))
                rows.append(value)
                if code is codes[1]:
                    assert frame.f_locals["row"] is value
        else:
            assert type(value) is bytes and value == payload
            row = frame.f_locals["row"]
            assert type(row) is Row and row.payload is value
            bind_result(results[-1][0], row)
            held.append((row, row._parent, row._data, value))
        returned.append((index, int(value is None)))

    def unwind(code, offset, error):
        if not any(code is known for known in codes):
            return
        phase = 0
        try:
            frame = helper._LIFETIME_FRAME(1)
            phase = 1
            parent = reader_parent(frame, code)
            if parent is None:
                return
            index = next(index for index, known in enumerate(codes) if code is known)
            unwind_attempts.append((index, int(active), int(frame.f_code is code), frame.f_lasti, offset, int(code is codes[3])))
            # Raw delivery operands are test-owned until caught-error identity
            # is checked. They are not accepted native receipts yet.
            unwind_errors.append(error)
            phase = 2
            sources_current()
            phase = 3
            bind_reader(parent)
            phase = 4
            assert active and frame.f_code is code
            candidate_frame = frame.f_back if opposition == "frame" else frame
            candidate_offset = wrong_offsets[opposition] if opposition in wrong_offsets else offset
            if opposition == "frame":
                assert candidate_frame is not None and candidate_frame is not frame and candidate_frame.f_code is not code
            elif opposition != "original":
                assert candidate_offset != offset
            expected_pair = (direct_raise, direct_raise) if replacement == "unwind" else propagated_pair
            candidates.append((frame.f_lasti, candidate_frame.f_lasti, candidate_offset, int(candidate_frame is frame)))
            _pg_require_unwind_site(frame, candidate_frame, code, candidate_offset, *expected_pair)
            phase = 5
            assert code is codes[3]
            # Preserve the actual unwinding primary even if these calibration
            # premises fail. Assert this invalid fact outside the callback.
            unwound.append(error)
        except BaseException as observer:
            invalid_unwind.append((phase, type(observer).__name__))
            message = "native unwind candidate frame changed" if opposition == "frame" else "native unwind candidate offset changed"
            if opposition != "original" and phase == 4 and type(observer) is AssertionError and observer.args == (message,):
                opposed.append(opposition)

    api = (
        helper._LIFETIME_GET_TOOL,
        helper._LIFETIME_GET_EVENTS,
        helper._LIFETIME_GET_LOCAL,
        helper._LIFETIME_REGISTER,
        helper._LIFETIME_USE_TOOL,
        helper._LIFETIME_SET_EVENTS,
        helper._LIFETIME_SET_LOCAL,
        helper._LIFETIME_FREE_TOOL,
    )
    monitoring = sys.monitoring
    assert monitoring is helper._LIFETIME_NAMESPACE
    current_api = (
        monitoring.get_tool,
        monitoring.get_events,
        monitoring.get_local_events,
        monitoring.register_callback,
        monitoring.use_tool_id,
        monitoring.set_events,
        monitoring.set_local_events,
        monitoring.free_tool_id,
    )
    assert all(original is current for original, current in zip(api, current_api, strict=True))
    before = tuple((tool, api[0](tool), api[1](tool), tuple(api[2](tool, code) for code in codes)) for tool in (3, 4))
    available = [tool for tool, owner, mask, local in before if owner is None and mask == 0 and local == (0, 0, 0, 0)]
    assert available, "calibration must own an originally free tool"
    tool = available[0]
    name = "pg-private-original-return-" + str(id(returned))
    deliveries = ((monitoring.events.PY_RETURN, deliver), (monitoring.events.PY_UNWIND, unwind))
    registered, local_masks = [], []
    uncertain_callbacks = set()
    owned = False
    listener = False
    profiled = False
    claim_attempted = False
    listener_attempted = False
    profile_attempted = False
    global_mask = 0
    cleanup = []
    try:
        claim_attempted = True
        api[4](tool, name)
        owned = True
        for number, callback in deliveries:
            uncertain_callbacks.add(number)
            previous = api[3](tool, number, callback)
            registered.append((number, previous, callback))
            uncertain_callbacks.remove(number)
            assert previous is None, "a freed tool with a retained callback is not empty"
        for code in codes:
            assert api[2](tool, code) == 0
            local_masks.append(code)
            api[6](tool, code, monitoring.events.PY_RETURN)
        global_mask = monitoring.events.PY_UNWIND
        api[5](tool, global_mask)
        listener_attempted = True
        event.listen(engine, "after_execute", after_execute)
        listener = True
        profile_attempted = True
        sys.setprofile(profile)
        profiled = True
        # A manual invocation with a genuine code object must not imitate a
        # native frame delivery. No actual producer or callback is replaced.
        active = True
        with pytest.raises(AssertionError, match="native delivery must enter"):
            deliver(codes[3], 0, payload)
        assert not returned and not held and not mutations
        with locked_session_transaction(engine, session_id) as conn:
            current_connection = conn
            assert store.require_continuation_mutation(conn, context) is True
            if replacement in ("unwind", "raised"):
                missing = PrivateChunkRef.model_validate(
                    {"codec": reference.codec, "digest": "0" * 64, "byte_length": reference.byte_length}
                )
                assert missing.digest != reference.digest
                expected_type = AuditIntegrityError if marker is None else marker_type
                current_reference = missing if marker is None else reference
                with pytest.raises(expected_type) as caught:
                    reader(
                        conn,
                        session_id=session_id,
                        operation_id=operation_id,
                        reference=current_reference,
                    )
                record_property("pg_private_unwind_attempts", tuple(unwind_attempts))
                record_property("pg_private_unwind_invalid", tuple(invalid_unwind))
                record_property("pg_private_unwind_error_identity", tuple(int(error is caught.value) for error in unwind_errors))
                record_property("pg_private_unwind_candidates", tuple(candidates))
                if opposition == "original":
                    assert len(unwound) == 1 and unwound[0] is caught.value and not invalid_unwind and not opposed
                else:
                    assert opposed == [opposition] and not unwound and invalid_unwind == [(4, "AssertionError")]
                    assert len(unwind_errors) == 1 and unwind_errors[0] is caught.value
                if marker is None:
                    assert str(caught.value) == "private chunk complete byte bound or SQL binding mismatch"
                    assert returned == [(0, 1), (1, 1), (2, 1)]
                else:
                    assert caught.value is marker and not returned
                assert not byte_calls and not rows and not mutations and not callbacks
                assert len(results) == 1
            else:
                for _ in range(2):
                    actual = reader(conn, session_id=session_id, operation_id=operation_id, reference=reference)
                    assert type(actual) is bytes and actual == payload
                assert returned == [(0, 0), (0, 1), (1, 0), (2, 0), (3, 0)] * 2
                assert byte_calls == ["len", "sha256"] * 2 and not unwound and not invalid_unwind
                assert len(rows) == 4 and rows[0] is rows[1] and rows[2] is rows[3]
                assert len(results) == 2
                assert len(mutations) == int(replacement in ("row", "data", "payload"))
                assert len(callbacks) == (2 if replacement == "callback" else 0)
                if mutations or callbacks:
                    witnesses = [item for item in held if len(item) == 6]
                    assert len(witnesses) == (2 if replacement == "callback" else 1)
                    for row, parent, data, original_payload, copied, copied_data in witnesses:
                        assert row._parent is parent and copied._parent is parent
                        assert data[4] is original_payload and copied._data is copied_data
                        assert copied.payload == original_payload == payload
                        if replacement in ("row", "callback"):
                            assert copied is not row
                        else:
                            assert copied is row and copied_data is not data
                        assert (copied.payload is not original_payload) is (replacement == "payload")
        record_property("pg_private_return_delivery", tuple(returned))
        record_property("pg_private_calibration", (len(mutations), len(callbacks), len(unwound), len(byte_calls), len(rows)))
        sources_current()
    finally:
        primary = sys.exception()
        active = False
        if profile_attempted:
            try:
                current_profile = sys.getprofile()
                if current_profile is profile:
                    sys.setprofile(prior)
                elif current_profile is not prior or profiled:
                    raise RuntimeError("private calibration profile owner changed")
            except BaseException as error:
                cleanup.append(error)
        if listener_attempted:
            try:
                if event.contains(engine, "after_execute", after_execute):
                    event.remove(engine, "after_execute", after_execute)
                elif listener:
                    raise RuntimeError("private calibration listener owner changed")
            except BaseException as error:
                cleanup.append(error)
        if replacement == "callback":
            for result, _context, _cursor, present, _metadata, _keymap in reversed(results):
                try:
                    if result._post_creational_filter is not copy_row:
                        raise RuntimeError("private calibration result callback owner changed")
                    if present:
                        result._post_creational_filter = None
                    else:
                        del result._post_creational_filter
                except BaseException as error:
                    cleanup.append(error)
        for result, _context, _cursor, _present, _metadata, _keymap in reversed(results):
            try:
                if not result.closed:
                    result.close()
            except BaseException as error:
                cleanup.append(error)
        monitor_cleanup = []
        tool_owned = False
        if claim_attempted:
            try:
                current_owner = api[0](tool)
                tool_owned = current_owner == name
                if not tool_owned and (current_owner is not None or owned):
                    raise RuntimeError("private calibration tool owner changed")
            except BaseException as error:
                monitor_cleanup.append(error)
        if tool_owned:
            try:
                if api[1](tool) not in (0, global_mask):
                    raise RuntimeError("private calibration global mask owner changed")
                api[5](tool, 0)
            except BaseException as error:
                monitor_cleanup.append(error)
            for code in reversed(local_masks):
                try:
                    if api[2](tool, code) not in (0, monitoring.events.PY_RETURN):
                        raise RuntimeError("private calibration local mask owner changed")
                    api[6](tool, code, 0)
                except BaseException as error:
                    monitor_cleanup.append(error)
            if uncertain_callbacks:
                monitor_cleanup.append(RuntimeError("private calibration callback exchange is uncertain"))
            for number, previous, callback in reversed(registered):
                try:
                    displaced = api[3](tool, number, previous)
                    if displaced is not callback:
                        api[3](tool, number, displaced)
                        raise RuntimeError("private calibration callback owner changed")
                except BaseException as error:
                    monitor_cleanup.append(error)
            if not monitor_cleanup:
                try:
                    api[7](tool)
                except BaseException as error:
                    monitor_cleanup.append(error)
        cleanup.extend(monitor_cleanup)
        if cleanup:
            if primary is not None:
                for error in cleanup:
                    primary.add_note("private calibration cleanup: " + type(error).__name__)
            else:
                raise BaseExceptionGroup("private calibration cleanup incomplete", cleanup)
        if primary is not None and invalid_unwind:
            primary.add_note("private calibration native unwind invalid")
    assert before == tuple((number, api[0](number), api[1](number), tuple(api[2](number, code) for code in codes)) for number in (3, 4))
    assert sys.getprofile() is prior and not event.contains(engine, "after_execute", after_execute)


def test_structure_internal_error_label_bypasses_exception_metaclass():
    opaque = object()

    class NameHook(type):
        def __getattribute__(cls, name):
            if name == "__name__":
                return opaque
            return type.__getattribute__(cls, name)

    class OriginalError(Exception, metaclass=NameHook):
        pass

    error = OriginalError()
    assert type(error).__name__ is opaque, "the ordinary type-name lookup must actually be opposed"
    # This is the actual diagnostic and thread buffer, with no observer/tool
    # installed. An absent traceback must be labelled unbound, never invented.
    trace = object.__new__(RetentionMemoTrace)
    thread = helper._StructureThread(threading.get_ident())
    trace._structure_threads = {thread.identity: thread}
    trace._structure_observer_incomplete = False
    helper._structure_internal_error(trace, error)
    assert thread.internal_errors == [("OriginalError", "unbound", 0)]
    assert all(type(field) is str for field in thread.internal_errors[0][:2])
    assert type(thread.internal_errors[0][2]) is int
    assert trace._structure_observer_incomplete is False


def test_structure_class_namespace_is_live_and_its_origin_is_immutable():
    class Original:
        pass

    class OtherMeta(type):
        pass

    namespace = Original.__dict__
    assert type(Original) is type and type(namespace) is MappingProxyType
    assert all(type(owner) is type for owner, *_facts in helper._STRUCTURE_STORE_SLOT_GUARDS)
    with pytest.raises(TypeError, match="__class__ assignment"):
        Original.__class__ = OtherMeta
    with pytest.raises(AttributeError, match="not writable"):
        Original.__dict__ = {}
    with pytest.raises(TypeError, match="__class__ assignment"):
        namespace.__class__ = dict
    Original.observed = None
    try:
        assert "observed" in namespace and namespace.get("observed") is None
    finally:
        del Original.observed
    assert "observed" not in namespace and namespace.get("observed") is None


def test_structure_store_slot_live_namespace_refuses_transient_absent_slot():
    matches = [fact for fact in helper._STRUCTURE_STORE_SLOT_GUARDS if fact[0] is Insert and fact[1] == "__getattr__"]
    assert len(matches) == 1 and matches[0][2:] == (False, None, None, None)
    assert not any(owner is Insert and name == "__getattr__" for owner, name, *_facts in helper._STRUCTURE_CACHE_SLOT_GUARDS)
    view = Insert.__dict__
    assert "__getattr__" not in view
    assert helper._structure_sql_sources_current() is True
    Insert.__getattr__ = None
    try:
        assert "__getattr__" in view and view.get("__getattr__") is None
        assert helper._structure_store_cache_sources_current() is True, "the actual store-slot predicate must be reached"
        assert helper._structure_sql_sources_current() is False
    finally:
        del Insert.__getattr__
    assert helper._structure_sql_sources_current() is True


@pytest.mark.parametrize("mutation", ["dict", "dict-subclass", "contains-retarget"])
def test_structure_store_slot_reads_actual_replaced_backing_dictionary(record_property, mutation):
    # Source-derived public APIs: GenericGetDict returns a NEW reference, while
    # GenericSetDict can replace even an exact-type class's actual tp_dict.
    get_dict = ctypes.PYFUNCTYPE(ctypes.c_void_p, ctypes.py_object, ctypes.c_void_p)(("PyObject_GenericGetDict", ctypes.pythonapi))
    set_dict = ctypes.PYFUNCTYPE(ctypes.c_int, ctypes.py_object, ctypes.py_object, ctypes.c_void_p)(
        ("PyObject_GenericSetDict", ctypes.pythonapi)
    )
    decref = ctypes.PYFUNCTYPE(None, ctypes.c_void_p)(("Py_DecRef", ctypes.pythonapi))
    modified = ctypes.PYFUNCTYPE(None, ctypes.py_object)(("PyType_Modified", ctypes.pythonapi))

    def actual_dictionary():
        address = get_dict(Insert, None)
        assert address
        try:
            return ctypes.cast(address, ctypes.py_object).value
        finally:
            decref(address)

    matches = [fact for fact in helper._STRUCTURE_STORE_SLOT_GUARDS if fact[0] is Insert and fact[1] == "__getattr__"]
    assert len(matches) == 1 and matches[0][2:] == (False, None, None, None)
    assert not any(owner is Insert and name == "__getattr__" for owner, name, *_facts in helper._STRUCTURE_CACHE_SLOT_GUARDS)
    original, old_view = actual_dictionary(), Insert.__dict__
    assert type(original) is dict and type(old_view) is MappingProxyType and "__getattr__" not in original
    assert helper._structure_sql_sources_current() is True
    get_calls = []
    contains_calls = []
    foreign = object()
    retargeted = dict(original)
    retargeted["__getattr__"] = foreign

    class DifferentGet(dict):
        def __contains__(self, key):
            if mutation == "contains-retarget" and key == "__getattr__":
                contains_calls.append(1)
                assert set_dict(Insert, retargeted, None) == 0
                modified(Insert)
                return False
            return super().__contains__(key)

        def get(self, key, default=None):
            if key == "__getattr__":
                get_calls.append(1)
                return foreign if mutation == "dict-subclass" else None
            return super().get(key, default)

    changed = dict(original) if mutation == "dict" else DifferentGet(original)
    if mutation == "dict":
        changed["__getattr__"] = None
    try:
        assert set_dict(Insert, changed, None) == 0
        modified(Insert)
        assert actual_dictionary() is changed and actual_dictionary() is not original
        assert "__getattr__" not in old_view, "the old proxy is stale after real backing replacement"
        assert helper._structure_store_cache_sources_current() is True, "the actual store-slot predicate must be reached"
        assert helper._structure_sql_sources_current() is False
        assert bool(get_calls) is (mutation == "dict-subclass"), "the second lookup must reacquire the current class dictionary"
        assert bool(contains_calls) is (mutation == "contains-retarget")
        assert actual_dictionary() is (retargeted if mutation == "contains-retarget" else changed)
        record_property("store_slot_backing_replacement", (int(mutation != "dict"), 1, len(get_calls), len(contains_calls), 1))
    finally:
        primary = sys.exception()
        errors = []
        try:
            assert set_dict(Insert, original, None) == 0
        except BaseException as error:
            errors.append(error)
        try:
            modified(Insert)
        except BaseException as error:
            errors.append(error)
        if errors:
            if primary is None:
                raise BaseExceptionGroup("actual class dictionary restoration incomplete", errors)
            for error in errors:
                primary.add_note("class dictionary restoration: " + type(error).__name__)
    assert actual_dictionary() is original and "__getattr__" not in Insert.__dict__
    assert helper._structure_sql_sources_current() is True


def test_structure_store_slot_expected_kind_tuple_binding_stays_original(monkeypatch):
    original = helper._STRUCTURE_GUARD_KIND_ORIGINS
    assert helper._STRUCTURE_STORE_SLOT_KIND_ORIGINS is original
    copied = (*original,)
    assert copied is not original and all(a is b for a, b in zip(copied, original, strict=True))
    assert helper._structure_sql_sources_current() is True
    with monkeypatch.context() as patcher:
        patcher.setattr(helper, "_STRUCTURE_GUARD_KIND_ORIGINS", copied)
        assert helper._structure_store_cache_sources_current() is True, "the optimized store-slot block must be reached"
        assert helper._structure_sql_sources_current() is False
    assert helper._structure_sql_sources_current() is True
