"""Real SQLite/PostgreSQL JSON projection and unchanged dispatch integrity."""

from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import pytest
from sqlalchemy import JSON, Column, Connection, MetaData, String, Table, create_engine, event, insert

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.audit import begin_dispatch, finish_success
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.sessions.dispatch_evidence import _PipelineDispatchEvidenceRead, read_dispatch_envelopes
from elspeth.web.sessions.proposal_authority import _persisted_pipeline_dispatch_content_hashes
from tests.helpers.postgres_target import postgres_test_target

# Deliberately minimal SQL fixture: these tests qualify JSON projection, not
# session authorization. Existing service/route tests cover the full lifecycle.
_ROWS = Table(
    "chat_messages",
    MetaData(),
    Column("id", String, primary_key=True),
    Column("session_id", String, nullable=False, index=True),
    Column("tool_calls", JSON),
)


@pytest.fixture(
    scope="module",
    params=[
        "sqlite",
        pytest.param("postgresql", marks=pytest.mark.testcontainer),
        pytest.param("postgresql-psycopg2", marks=pytest.mark.testcontainer),
    ],
)
def database_url(request: pytest.FixtureRequest) -> Iterator[str]:
    if request.param == "sqlite":
        yield "sqlite:///:memory:"
    else:
        with postgres_test_target(driver="psycopg2" if request.param == "postgresql-psycopg2" else "psycopg") as url:
            yield url


@pytest.fixture
def connection(database_url: str) -> Iterator[Connection]:
    engine = create_engine(database_url)
    _ROWS.metadata.create_all(engine)
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                yield conn
            finally:
                if transaction.is_active:
                    transaction.rollback()
    finally:
        _ROWS.metadata.drop_all(engine)
        engine.dispose()


def _write(conn: Connection, row_id: str, calls: object, *, session_id: str = "session") -> None:
    conn.execute(insert(_ROWS).values(id=row_id, session_id=session_id, tool_calls=calls))


def _evidence(call_id: str = "call") -> tuple[dict[str, Any], PipelineDispatchAuditBinding]:
    audit = begin_dispatch(
        call_id, "set_pipeline", {"sources": {}, "nodes": [], "edges": [], "outputs": []}, version_before=0, actor="test"
    )
    invocation = finish_success(
        audit,
        result_payload={
            "success": True,
            "pipeline_content_hash_schema": "composer.pipeline-dispatch-result.v1",
            "pipeline_content_hash": "a" * 64,
            "validation": {
                "is_valid": True,
                "errors": [],
                "warnings": [],
                "suggestions": [],
                "semantic_contracts": [],
                "graph_repair_suggestions": [],
            },
            "affected_nodes": [],
            "version": 1,
        },
        version_after=1,
    )
    _, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    return envelope, PipelineDispatchAuditBinding.from_persisted_envelope(envelope)


def test_exact_candidate_projection_preserves_duplicates_and_excludes_large_siblings(connection: Connection) -> None:
    envelope, dispatch = _evidence('123"é')
    noise = {"_kind": "provider", "payload": "x" * 131072}
    _write(connection, "row", [noise, envelope, envelope, {"invocation": {"tool_call_id": 123}}, "not JSON"])
    _write(connection, "other", [envelope], session_id="other-session")
    for index, value in enumerate((None, "string", 123, {}, {"invocation": {"tool_call_id": dispatch.tool_call_id}})):
        _write(connection, f"non-array-{index}", value)
    candidates = read_dispatch_envelopes(connection, session_id="session", tool_call_ids=(dispatch.tool_call_id,))
    assert candidates == ((dispatch.tool_call_id, envelope), (dispatch.tool_call_id, envelope))
    assert _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=dispatch) == ("a" * 64, "a" * 64)
    assert read_dispatch_envelopes(connection, session_id="session", tool_call_ids=("missing",)) == ()


@pytest.mark.parametrize("corruption", ["kind", "tool", "status", "missing-status", "result", "arguments", "content-schema"])
def test_malformed_matching_evidence_is_not_hidden(connection: Connection, corruption: str) -> None:
    envelope, dispatch = _evidence()
    damaged = deepcopy(envelope)
    invocation = damaged["invocation"]
    if corruption == "kind":
        damaged["_kind"] = "provider"
    elif corruption == "tool":
        invocation["tool_name"] = "other_tool"
    elif corruption == "status":
        invocation["status"] = "invalid"
    elif corruption == "missing-status":
        del invocation["status"]
    elif corruption == "result":
        invocation["result_canonical"] = "{broken"
    elif corruption == "arguments":
        invocation["arguments_hash"] = "b" * 64
    else:
        from elspeth.core.canonical import canonical_json, stable_hash

        payload = {"pipeline_content_hash_schema": "wrong", "pipeline_content_hash": "a" * 64}
        invocation["result_canonical"] = canonical_json(payload)
        invocation["result_hash"] = stable_hash(payload)
    _write(connection, "valid", [envelope])
    _write(connection, "damaged", [damaged])
    with pytest.raises(AuditIntegrityError):
        _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=dispatch)
    batched = _PipelineDispatchEvidenceRead(connection, session_id="session", tool_call_ids=(dispatch.tool_call_id,))
    with pytest.raises(AuditIntegrityError):
        _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=dispatch, evidence_read=batched)


def test_batch_reads_once_and_preserves_duplicate_and_wrong_binding_checks(connection: Connection) -> None:
    first, first_binding = _evidence("first")
    second, second_binding = _evidence("second")
    _write(connection, "one", [first, second])
    _write(connection, "two", [first])
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        batch = _PipelineDispatchEvidenceRead(connection, session_id="session", tool_call_ids=("first", "second"))
        assert _persisted_pipeline_dispatch_content_hashes(
            connection, session_id="session", dispatch=first_binding, evidence_read=batch
        ) == ("a" * 64, "a" * 64)
        assert _persisted_pipeline_dispatch_content_hashes(
            connection, session_id="session", dispatch=second_binding, evidence_read=batch
        ) == ("a" * 64,)
        assert len(statements) == (3 if connection.dialect.name == "postgresql" else 1)
        assert sum(statement.lstrip().upper().startswith("SELECT") for statement in statements) == 1
        from dataclasses import replace

        with pytest.raises(AuditIntegrityError, match="terminal binding"):
            _persisted_pipeline_dispatch_content_hashes(
                connection, session_id="session", dispatch=replace(second_binding, result_hash="b" * 64), evidence_read=batch
            )
        with pytest.raises(AuditIntegrityError, match="escaped"):
            batch.envelopes_for(connection, session_id="other", tool_call_id="first")
        connection.rollback()
        with pytest.raises(AuditIntegrityError, match="escaped"):
            batch.envelopes_for(connection, session_id="session", tool_call_id="first")
    finally:
        event.remove(connection, "before_cursor_execute", capture)


def test_identity_types_and_failed_attempts_preserve_old_reader_semantics(connection: Connection) -> None:
    from sqlalchemy import select

    from elspeth.web.sessions.proposal_authority import _pipeline_dispatch_recovery_from_envelope

    envelope, binding = _evidence("123")
    failed = deepcopy(envelope)
    failed["invocation"]["status"] = "cancelled"
    damaged_unrelated = {"invocation": {"tool_call_id": "unrelated"}, "_kind": "wrong"}
    _write(
        connection,
        "row",
        [
            {"invocation": {"tool_call_id": 123}},
            {"invocation": ["123"]},
            {"invocation": "123"},
            {"invocation": None},
            False,
            123,
            None,
            failed,
            damaged_unrelated,
            envelope,
        ],
    )
    # The old full-array reader is an independent selection oracle: it sees
    # every stored slot, with no JSON SQL predicate shared with the candidate.
    expected: list[str] = []
    for calls in connection.execute(select(_ROWS.c.tool_calls)).scalars():
        if type(calls) is not list:
            continue
        for slot in calls:
            recovery = _pipeline_dispatch_recovery_from_envelope(slot, expected_tool_call_id="123")
            if recovery is not None:
                assert recovery.binding == binding
                expected.append(recovery.executor_content_hash)
    assert expected == ["a" * 64]
    assert _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=binding) == tuple(expected)
    assert len(read_dispatch_envelopes(connection, session_id="session", tool_call_ids=("123",))) == 2


def test_a_new_read_in_same_transaction_sees_new_dispatch_rows(connection: Connection) -> None:
    envelope, binding = _evidence()
    _write(connection, "first", [envelope])
    first = _PipelineDispatchEvidenceRead(connection, session_id="session", tool_call_ids=("call",))
    assert _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=binding, evidence_read=first) == (
        "a" * 64,
    )
    _write(connection, "later", [envelope])
    # Production constructs a fresh private batch inside each synchronous
    # lifecycle-list verification, not once per connection or transaction.
    second = _PipelineDispatchEvidenceRead(connection, session_id="session", tool_call_ids=("call",))
    assert _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=binding, evidence_read=second) == (
        "a" * 64,
        "a" * 64,
    )
    assert _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=binding) == ("a" * 64, "a" * 64)


@pytest.mark.parametrize(
    "raw",
    [
        '[{"invocation":{"tool_call_id":"elsewhere","tool_call_id":"call"}}]',
        '[{"invocation":"wrong","invocation":{"tool_call_id":"call"}}]',
        '[{"invocation":{"tool_call_id":"call","tool_call_id":"call"}}]',
    ],
)
def test_duplicate_json_keys_cannot_hide_malformed_matching_evidence(connection: Connection, raw: str) -> None:
    from sqlalchemy import text

    _, binding = _evidence()
    # Bypass the JSON serializer deliberately to model corrupt durable JSON
    # whose duplicate keys remain legal in SQLite JSON and PostgreSQL JSON.
    connection.execute(
        text("INSERT INTO chat_messages (id, session_id, tool_calls) VALUES (:id, :sid, :calls)"),
        {"id": "raw", "sid": "session", "calls": raw},
    )
    candidates = read_dispatch_envelopes(connection, session_id="session", tool_call_ids=("call",))
    assert len(candidates) == 1
    with pytest.raises(AuditIntegrityError):
        _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=binding)


def test_last_duplicate_json_key_remains_unrelated(connection: Connection) -> None:
    from sqlalchemy import text

    raw = '[{"invocation":{"tool_call_id":"call","tool_call_id":"elsewhere"}}]'
    connection.execute(
        text("INSERT INTO chat_messages (id, session_id, tool_calls) VALUES (:id, :sid, :calls)"),
        {"id": "raw", "sid": "session", "calls": raw},
    )
    assert read_dispatch_envelopes(connection, session_id="session", tool_call_ids=("call",)) == ()


@pytest.mark.parametrize(
    "noise",
    [
        '[{"_kind":"provider","payload":"\\u0000"}]',
        '[{"invocation":{"tool_call_id":"unrelated","arguments":{"text":"\\u0000"}}}]',
        '[{"_kind":"provider","payload":1e100000}]',
        '[{"_kind":"provider","payload":1e1000000}]',
        '[{"_kind":"provider","payload":1e-1000000}]',
        '[{"_kind":"provider","payload":"\\ud800"}]',
        '[{"_kind":"provider","payload":"\\udc00"}]',
        '[{"_kind":"provider","payload":"\\ud83d\\ude00"}]',
        '[{"_kind":"provider","payload":"ordinary"}]',
    ],
)
@pytest.mark.parametrize("same_row", [False, True])
def test_postgres_legacy_json_storage_domain(connection: Connection, noise: str, same_row: bool) -> None:
    """Driver-decoded full scan is the independent old-reader domain control."""
    import json

    from sqlalchemy import select, text
    from sqlalchemy.exc import DBAPIError

    from elspeth.web.sessions.proposal_authority import _pipeline_dispatch_recovery_from_envelope

    if connection.dialect.name != "postgresql":
        pytest.skip("PostgreSQL JSON versus JSONB domain control")
    envelope, binding = _evidence()
    if same_row:
        raw = "[" + json.dumps(envelope) + "," + noise[1:]
    else:
        _write(connection, "matching", [envelope])
        raw = noise
    connection.execute(
        text("INSERT INTO chat_messages (id, session_id, tool_calls) VALUES ('noise', 'session', CAST(:raw AS JSON))"), {"raw": raw}
    )
    # Fetch through the real JSON driver path before exercising the projection.
    rows = connection.execute(select(_ROWS.c.tool_calls).where(_ROWS.c.session_id == "session")).scalars().all()
    expected = []
    for calls in rows:
        for slot in calls:
            recovered = _pipeline_dispatch_recovery_from_envelope(slot, expected_tool_call_id=binding.tool_call_id)
            if recovered is not None:
                assert recovered.binding == binding
                expected.append(recovered.executor_content_hash)
    assert expected == ["a" * 64]
    try:
        with connection.begin_nested():
            actual = _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=binding)
    except DBAPIError as error:
        raise AssertionError(f"projection rejected legacy-readable JSON: {type(error.orig).__name__}") from None
    assert actual == tuple(expected)


@pytest.mark.parametrize("call_id", ["call\x00suffix", "call\ud800suffix", "call\udc00suffix"])
def test_nonbindable_id_uses_exact_legacy_identity(connection: Connection, call_id: str) -> None:
    from sqlalchemy import select

    envelope, _ = _evidence()
    envelope["invocation"]["tool_call_id"] = call_id
    binding = PipelineDispatchAuditBinding.from_persisted_envelope(envelope)
    _write(connection, "matching", [envelope, envelope, {"invocation": {"tool_call_id": 123}}])
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=binding) == ("a" * 64, "a" * 64)
        assert len(statements) == 1
        assert "jsonb" not in statements[0].lower()
        assert "json_each" not in statements[0].lower()
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert connection.execute(select(_ROWS.c.id)).scalar_one() == "matching"


def test_pg_domain_fallback_retains_outer_writes_duplicates_and_exact_types(connection: Connection) -> None:
    from sqlalchemy import select, text

    if connection.dialect.name != "postgresql":
        pytest.skip("PostgreSQL savepoint recovery control")
    envelope, binding = _evidence("123")
    outer = connection.get_transaction()
    _write(connection, "pending-write", [envelope, envelope, {"invocation": {"tool_call_id": 123}}])
    connection.execute(
        text("INSERT INTO chat_messages VALUES ('noise', 'session', CAST(:raw AS JSON))"), {"raw": '[{"payload":"\\u0000"}]'}
    )
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=binding) == ("a" * 64, "a" * 64)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert connection.get_transaction() is outer and outer.is_active
    assert any(statement.startswith("ROLLBACK TO SAVEPOINT") for statement in statements)
    assert len(statements) == 4  # savepoint, rejected projection, rollback, legacy SELECT
    assert connection.execute(select(_ROWS.c.id).where(_ROWS.c.id == "pending-write")).scalar_one() == "pending-write"
    from dataclasses import replace

    with pytest.raises(AuditIntegrityError, match="terminal binding"):
        _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=replace(binding, result_hash="b" * 64))


@pytest.mark.parametrize("noise", ['[ {"payload":"\\u0000"} ]', '[ {"payload":"\\ud800"} ]', '[ {"payload":"\\udc00"} ]'])
@pytest.mark.parametrize("corruption", ["kind", "status", "missing-invocation", "arguments"])
def test_pg_fallback_does_not_hide_malformed_matching_evidence(connection: Connection, corruption: str, noise: str) -> None:
    from sqlalchemy import text

    if connection.dialect.name != "postgresql":
        pytest.skip("PostgreSQL JSON-domain fallback integrity control")
    envelope, binding = _evidence()
    damaged = deepcopy(envelope)
    if corruption == "kind":
        damaged["_kind"] = "provider"
    elif corruption == "status":
        damaged["invocation"]["status"] = "bad"
    elif corruption == "missing-invocation":
        # Keep identity available but remove another required matching field.
        del damaged["invocation"]["tool_name"]
    else:
        damaged["invocation"]["arguments_hash"] = "b" * 64
    _write(connection, "matches", [envelope, damaged])
    connection.execute(text("INSERT INTO chat_messages VALUES ('noise', 'session', CAST(:raw AS JSON))"), {"raw": noise})
    with pytest.raises(AuditIntegrityError):
        _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=binding)


@pytest.mark.parametrize(
    "sql, sqlstate", [("SELECT definitely_missing_dispatch_column FROM chat_messages", "42703"), ("SELECT 1 / 0", "22012")]
)
def test_pg_unrelated_sql_error_is_not_a_fallback(connection: Connection, monkeypatch: pytest.MonkeyPatch, sql: str, sqlstate: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    import elspeth.web.sessions.dispatch_evidence as module

    if connection.dialect.name != "postgresql":
        pytest.skip("PostgreSQL SQLSTATE propagation control")

    def fail(conn, **kwargs):
        return conn.execute(text(sql)).all()

    def forbidden(*args, **kwargs):
        raise AssertionError("unrelated SQL error must not invoke legacy fallback")

    monkeypatch.setattr(module, "_read_projected_candidates", fail)
    monkeypatch.setattr(module, "_read_legacy_dispatch_envelopes", forbidden)
    with pytest.raises(DBAPIError) as result:
        read_dispatch_envelopes(connection, session_id="session", tool_call_ids=("call",))
    if connection.dialect.driver == "psycopg":
        assert result.value.orig.sqlstate == sqlstate
    else:
        assert result.value.orig.pgcode == sqlstate
    assert connection.execute(text("SELECT 1")).scalar_one() == 1


@pytest.mark.parametrize("call_id", ["call\x00suffix", "call\ud800suffix", "call\udc00suffix"])
def test_actual_tool_batch_admission_does_not_restrict_sql_text_domain(call_id: str) -> None:
    from types import SimpleNamespace

    from elspeth.web.composer.tool_batch import _admit_tool_batch

    admitted = _admit_tool_batch([SimpleNamespace(id=call_id, function=SimpleNamespace(name="set_pipeline", arguments="{}"))])
    assert admitted.calls[0].id == call_id


def test_sqlite_projection_import_does_not_require_postgres_drivers() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    script = """
import builtins
original_import = builtins.__import__
def without_postgres(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] in {'psycopg', 'psycopg2'}:
        raise ImportError('PostgreSQL extras deliberately unavailable')
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = without_postgres
for package in ('psycopg', 'psycopg2'):
    try:
        __import__(package)
    except ImportError:
        pass
    else:
        raise AssertionError('absolute driver import was not blocked')
from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, insert
from elspeth.web.sessions.dispatch_evidence import read_dispatch_envelopes
engine = create_engine('sqlite:///:memory:')
rows = Table('chat_messages', MetaData(), Column('id', String, primary_key=True), Column('session_id', String), Column('tool_calls', JSON))
rows.metadata.create_all(engine)
with engine.begin() as conn:
    envelope = {'invocation': {'tool_call_id': 'call'}}
    conn.execute(insert(rows).values(id='row', session_id='session', tool_calls=[envelope]))
    assert read_dispatch_envelopes(conn, session_id='session', tool_call_ids=('call',)) == (('call', envelope),)
engine.dispose()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("number", ["1e100000", "1.0", "1e2", "-0.0"])
@pytest.mark.parametrize("location", ["envelope", "invocation"])
def test_selected_envelope_retains_original_numeric_decoding(connection: Connection, number: str, location: str) -> None:
    import json

    from sqlalchemy import select, text

    from elspeth.web.sessions.proposal_authority import _pipeline_dispatch_recovery_from_envelope

    if connection.dialect.name != "postgresql":
        pytest.skip("PostgreSQL JSONB normalization control")
    envelope, binding = _evidence()
    raw = json.dumps([envelope])
    if location == "envelope":
        raw = raw.replace("[{", '[{"ignored_numeric": ' + number + ", ", 1)
    else:
        raw = raw.replace('"invocation": {', '"invocation": {"ignored_numeric": ' + number + ", ", 1)
    connection.execute(text("INSERT INTO chat_messages VALUES ('numeric', 'session', CAST(:raw AS JSON))"), {"raw": raw})
    old = connection.execute(select(_ROWS.c.tool_calls)).scalar_one()[0]
    recovered = _pipeline_dispatch_recovery_from_envelope(old, expected_tool_call_id=binding.tool_call_id)
    assert recovered is not None and recovered.binding == binding and recovered.executor_content_hash == "a" * 64
    candidates = read_dispatch_envelopes(connection, session_id="session", tool_call_ids=(binding.tool_call_id,))
    assert len(candidates) == 1
    returned = candidates[0][1]
    old_extra = old["ignored_numeric"] if location == "envelope" else old["invocation"]["ignored_numeric"]
    new_extra = returned["ignored_numeric"] if location == "envelope" else returned["invocation"]["ignored_numeric"]
    assert type(new_extra) is type(old_extra) and new_extra == old_extra
    assert _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=binding) == ("a" * 64,)


def test_original_slot_projection_keeps_first_middle_last_and_duplicates(connection: Connection) -> None:
    envelope, binding = _evidence()
    slots = []
    for marker in ("first", "middle", "last"):
        candidate = deepcopy(envelope)
        candidate["position_marker"] = marker
        slots.append(candidate)
    _write(connection, "positions", [slots[0], "scalar", {"invocation": {"tool_call_id": "different"}}, slots[1], False, None, slots[2]])
    projected = read_dispatch_envelopes(connection, session_id="session", tool_call_ids=(binding.tool_call_id,))
    assert [candidate["position_marker"] for _, candidate in projected] == ["first", "middle", "last"]
    assert _persisted_pipeline_dispatch_content_hashes(connection, session_id="session", dispatch=binding) == ("a" * 64,) * 3
