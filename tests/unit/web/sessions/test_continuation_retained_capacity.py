"""Direct SQL red controls for a caller-supplied retained-payload allowance."""

from __future__ import annotations

import pytest
from sqlalchemy import event, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, load_snapshot, put_chunk
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.unit.web.sessions.test_continuation_schema import _retained_payload_rows, _snapshot_rows

pytest_plugins = ("tests.unit.web.sessions.test_continuation_schema",)


def _used(connection, sid: str, operation_id: str) -> int:
    return sum(size for _codec, size in _retained_payload_rows(connection, sid, operation_id).values())


def test_exact_final_root_and_raw_event_allowance_is_admitted(snapshot_store):
    engine, sid, operation_id, _ = snapshot_store
    with engine.begin() as connection:
        current = load_snapshot(connection, session_id=sid, operation_id=operation_id)
        final = encode_continuation_snapshot(decode_continuation_snapshot(current.payload_json).model_copy(update={"cursor": 1}))
        event_json = '{"text":"café 雪"}'
        allowance = _used(connection, sid, operation_id) + len(final.encode("utf-8")) + len(event_json.encode("utf-8"))
        result = append_complete_snapshot(
            connection,
            session_id=sid,
            operation_id=operation_id,
            expected_checkpoint_id=current.checkpoint_id,
            expected_cursor=current.cursor,
            snapshot_json=current.payload_json,
            event_batch=(ContinuationEvent("checkpoint", event_json),),
            handoff=False,
            retained_payload_limit_bytes=allowance,
        )
        assert result.cursor == 1
        assert _used(connection, sid, operation_id) == allowance


def test_one_byte_short_refuses_complete_append_before_dml(snapshot_store):
    from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded

    engine, sid, operation_id, _ = snapshot_store
    writes: list[str] = []

    def observe(_connection, _cursor, statement, _parameters, context, _executemany):
        if context.isinsert or context.isupdate or context.isdelete:
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with engine.begin() as connection:
            put_chunk(
                connection,
                session_id=sid,
                operation_id=operation_id,
                key="dml-observer-positive",
                payload=b"{}",
                codec="event.v2",
            )
        assert writes, "DML observer must detect an actual INSERT"
        writes.clear()
        before = _snapshot_rows(engine)
        with pytest.raises(ContinuationRetainedCapacityExceeded), engine.begin() as connection:
            current = load_snapshot(connection, session_id=sid, operation_id=operation_id)
            final = encode_continuation_snapshot(decode_continuation_snapshot(current.payload_json).model_copy(update={"cursor": 1}))
            event_json = '{"text":"café 雪"}'
            allowance = _used(connection, sid, operation_id) + len(final.encode("utf-8")) + len(event_json.encode("utf-8")) - 1
            append_complete_snapshot(
                connection,
                session_id=sid,
                operation_id=operation_id,
                expected_checkpoint_id=current.checkpoint_id,
                expected_cursor=current.cursor,
                snapshot_json=current.payload_json,
                event_batch=(ContinuationEvent("checkpoint", event_json),),
                handoff=False,
                retained_payload_limit_bytes=allowance,
            )
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert writes == []
    assert _snapshot_rows(engine) == before


def test_all_historical_keys_count_and_exact_reuse_is_free(snapshot_store):
    from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded

    engine, sid, operation_id, _ = snapshot_store
    with engine.begin() as connection:
        initial = _used(connection, sid, operation_id)
        put_chunk(
            connection,
            session_id=sid,
            operation_id=operation_id,
            key="off-head-a",
            payload=b"{}",
            codec="event.v2",
            retained_payload_limit_bytes=initial + 2,
        )
        put_chunk(
            connection,
            session_id=sid,
            operation_id=operation_id,
            key="off-head-a",
            payload=b"{}",
            codec="event.v2",
            retained_payload_limit_bytes=initial + 2,
        )
        assert _used(connection, sid, operation_id) == initial + 2
        with pytest.raises(ContinuationRetainedCapacityExceeded):
            put_chunk(
                connection,
                session_id=sid,
                operation_id=operation_id,
                key="off-head-b",
                payload=b"{}",
                codec="event.v2",
                retained_payload_limit_bytes=initial + 2,
            )
        with pytest.raises(AuditIntegrityError, match="collision"):
            put_chunk(
                connection,
                session_id=sid,
                operation_id=operation_id,
                key="off-head-a",
                payload=b"[]",
                codec="event.v2",
                retained_payload_limit_bytes=initial + 2,
            )
        assert _used(connection, sid, operation_id) == initial + 2


def test_capacity_refusal_rolls_back_prior_chunk_in_original_transaction(snapshot_store):
    from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded

    engine, sid, operation_id, _ = snapshot_store
    before = _snapshot_rows(engine)
    with pytest.raises(ContinuationRetainedCapacityExceeded), engine.begin() as connection:
        initial = _used(connection, sid, operation_id)
        put_chunk(
            connection,
            session_id=sid,
            operation_id=operation_id,
            key="same-transaction-first",
            payload=b"{}",
            codec="event.v2",
            retained_payload_limit_bytes=initial + 2,
        )
        put_chunk(
            connection,
            session_id=sid,
            operation_id=operation_id,
            key="same-transaction-second",
            payload=b"{}",
            codec="event.v2",
            retained_payload_limit_bytes=initial + 2,
        )
    assert _snapshot_rows(engine) == before


def test_corrupt_stored_length_outranks_fresh_capacity_refusal(snapshot_store):
    engine, sid, operation_id, _ = snapshot_store
    with engine.begin() as connection:
        original = _used(connection, sid, operation_id)
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        try:
            connection.execute(
                update(chunks)
                .where(chunks.c.session_id == sid, chunks.c.operation_id == operation_id, chunks.c.chunk_key == "initial-snapshot")
                .values(byte_length=1)
            )
        finally:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
        with pytest.raises(AuditIntegrityError, match=r"retained.*length"):
            put_chunk(
                connection,
                session_id=sid,
                operation_id=operation_id,
                key="off-head-new",
                payload=b"{}",
                codec="event.v2",
                retained_payload_limit_bytes=original,
            )


def test_corrupt_off_head_digest_outranks_fresh_capacity_refusal(snapshot_store):
    engine, sid, operation_id, _ = snapshot_store
    with engine.begin() as connection:
        original = _used(connection, sid, operation_id)
        connection.execute(
            update(chunks)
            .where(chunks.c.session_id == sid, chunks.c.operation_id == operation_id, chunks.c.chunk_key == "initial-snapshot")
            .values(sha256="f" * 64)
        )
        with pytest.raises(AuditIntegrityError, match=r"retained.*digest"):
            put_chunk(
                connection,
                session_id=sid,
                operation_id=operation_id,
                key="off-head-new",
                payload=b"{}",
                codec="event.v2",
                retained_payload_limit_bytes=original,
            )


@pytest.mark.parametrize("invalid", (0, -1, True, 1.5))
def test_allowance_requires_exact_positive_integer(snapshot_store, invalid):
    engine, sid, operation_id, _ = snapshot_store
    with pytest.raises(ValueError, match="positive integer"), engine.begin() as connection:
        put_chunk(
            connection,
            session_id=sid,
            operation_id=operation_id,
            key="invalid-policy",
            payload=b"{}",
            codec="event.v2",
            retained_payload_limit_bytes=invalid,
        )
