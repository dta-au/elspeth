"""Current-policy bounded cold reads and immutable admission tuples."""

from collections.abc import Mapping
from hashlib import sha256

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.continuation import load_snapshot, put_chunk, read_chunk
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.unit.web.sessions.test_continuation_current_capacity import _admit_parent, _initialize
from tests.unit.web.sessions.test_continuation_schema import continuation_unadmitted_parent

continuation_parent = continuation_unadmitted_parent


def test_cold_root_refuses_actual_oversize_before_decode(continuation_parent, monkeypatch):
    from elspeth.web.composer import continuation_codec as codec

    engine, sid, operation = continuation_parent
    with engine.begin() as conn:
        policy, encoded = _admit_parent(conn, sid, operation)
        _initialize(conn, sid, operation, policy, encoded)
    exact = encoded.encode() + b" " * (policy.max_retained_payload_bytes - len(encoded.encode()))
    original = codec.decode_continuation_snapshot
    decoded = []

    def observe(payload):
        decoded.append(payload)
        return original(payload)

    monkeypatch.setattr(codec, "decode_continuation_snapshot", observe)
    for payload in (exact, exact + b" "):
        with engine.begin() as conn:
            conn.execute(
                update(chunks)
                .where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == "snapshot:initial-checkpoint")
                .values(payload=payload, byte_length=len(payload), sha256=sha256(payload).hexdigest())
            )
        decoded.clear()
        if payload is exact:
            with engine.connect() as conn:
                assert load_snapshot(conn, session_id=sid, operation_id=operation).payload_json == exact.decode()
            assert decoded
        else:
            with engine.connect() as conn, pytest.raises(AuditIntegrityError):
                load_snapshot(conn, session_id=sid, operation_id=operation)
            assert not decoded


def test_cold_event_chunk_uses_original_allowance(continuation_parent):
    engine, sid, operation = continuation_parent
    with engine.begin() as conn:
        policy, encoded = _admit_parent(conn, sid, operation)
        _initialize(conn, sid, operation, policy, encoded)
        for key, length in (("exact", policy.max_retained_payload_bytes), ("oversize", policy.max_retained_payload_bytes + 1)):
            payload = b"x" * length
            conn.execute(
                insert(chunks).values(
                    session_id=sid,
                    operation_id=operation,
                    chunk_key=key,
                    codec="event.v2",
                    payload=payload,
                    byte_length=length,
                    sha256=sha256(payload).hexdigest(),
                )
            )
        assert read_chunk(conn, session_id=sid, operation_id=operation, key="exact") == b"x" * policy.max_retained_payload_bytes
        with pytest.raises(AuditIntegrityError):
            read_chunk(conn, session_id=sid, operation_id=operation, key="oversize")


@pytest.mark.parametrize("path", ("collision", "overflow"))
def test_writer_history_conditionally_projects_corrupt_oversize(continuation_parent, path):
    engine, sid, operation = continuation_parent
    with engine.begin() as conn:
        policy, encoded = _admit_parent(conn, sid, operation)
        _initialize(conn, sid, operation, policy, encoded)
        raw = b"x" * (policy.max_retained_payload_bytes + 1)
        conn.execute(
            insert(chunks).values(
                session_id=sid,
                operation_id=operation,
                chunk_key="historical",
                codec="event.v2",
                payload=raw,
                byte_length=len(raw),
                sha256=sha256(raw).hexdigest(),
            )
        )
    statements = []

    def names_historical(parameters):
        if isinstance(parameters, Mapping):
            return "historical" in parameters.values()
        if isinstance(parameters, (tuple, list)):
            return "historical" in parameters
        return False

    def observe(_connection, _cursor, statement, parameters, _context, _executemany):
        if statement.lstrip().lower().startswith("select") and "composer_continuation_chunks" in statement and names_historical(parameters):
            statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(chunks.c.payload).where(
                        chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == "historical"
                    )
                ).scalar_one()
                == raw
            )
        assert len(statements) == 1 and "case" not in statements[0]
        statements.clear()
        with engine.begin() as conn, pytest.raises(AuditIntegrityError):
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key="historical" if path == "collision" else "fresh",
                payload=b"{}",
                codec="event.v2",
            )
        assert statements and all("case" in statement and "length" in statement for statement in statements)
    finally:
        event.remove(engine, "before_cursor_execute", observe)


@pytest.mark.parametrize("mutation", ("version", "hash", "allowance", "missing"))
def test_parent_policy_tuple_is_immutable(continuation_parent, mutation):
    engine, sid, operation = continuation_parent
    with engine.begin() as conn:
        policy, encoded = _admit_parent(conn, sid, operation)
        _initialize(conn, sid, operation, policy, encoded)
        original = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    changed = {
        "version": {"continuation_policy_version": "composer.continuation.policy.v3"},
        "hash": {"continuation_policy_sha256": "f" * 64},
        "allowance": {"continuation_retained_payload_limit_bytes": policy.max_retained_payload_bytes + 1},
        "missing": {"continuation_policy_version": None},
    }[mutation]
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation).values(**changed))
    with engine.begin() as conn:
        assert conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one() == original
        conn.execute(
            update(jobs)
            .where(jobs.c.session_id == sid, jobs.c.operation_id == operation)
            .values(
                continuation_policy_version=policy.schema_version,
                continuation_policy_sha256=policy.policy_hash,
                continuation_retained_payload_limit_bytes=policy.max_retained_payload_bytes,
                status_revision=original.status_revision + 1,
            )
        )
        assert load_snapshot(conn, session_id=sid, operation_id=operation).payload_json == encoded
