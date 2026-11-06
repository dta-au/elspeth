"""Current-policy SQL bounds using genuine initial admission and append DATA."""

from uuid import UUID

import pytest
from sqlalchemy import event

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import prepare_composer_continuation_admission
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    append_complete_snapshot,
    continuation_unpaid_terminal_reserve_on_connection,
    initialize_continuation,
    load_snapshot,
    prepare_snapshot_append,
    put_chunk,
)
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.sessions.test_continuation_schema import (
    _retained_payload_rows,
    _snapshot_rows,
    continuation_unadmitted_parent,
    insert_continuation_parent,
)

continuation_parent = continuation_unadmitted_parent


def _admit_parent(conn, sid, operation, *, limit=16_384):
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=limit))
    assert policy is not None
    seed = prepare_composer_continuation_admission(
        session_id=UUID(sid),
        operation_id=operation,
        kind="compose_message",
        request_json='{"content":"Build"}',
        request_hash="a" * 64,
        base_state_id=None,
        policy_hash=policy.policy_hash,
        admission_policy_json=policy.model_dump_json(),
        initial_trust_mode="auto_commit",
        initial_action_json=policy.actions.session_sql.model_dump_json(),
    )
    insert_continuation_parent(conn, sid, operation, policy_version=policy.schema_version, policy_hash=policy.policy_hash, limit=limit)
    return policy, seed.snapshot_json


def _initialize(conn, sid, operation, policy, encoded):
    initialize_continuation(
        conn,
        session_id=sid,
        operation_id=operation,
        checkpoint_id="initial-checkpoint",
        snapshot_json=encoded,
        action_json=policy.actions.session_sql.model_dump_json(),
        policy_sha256=policy.policy_hash,
    )


def _reserve(conn, sid, operation, encoded, cursor):
    return sum(
        len(chunk.payload)
        for chunk in continuation_unpaid_terminal_reserve_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            post_root_json=encoded,
            post_cursor=cursor,
        )
    )


def _observe_dml(engine, writes):
    def observed(_connection, _cursor, statement, _parameters, context, _executemany):
        if context.isinsert or context.isupdate or context.isdelete:
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", observed)
    return observed


def test_initial_capacity_preserves_actual_terminal_reserve_before_dml(continuation_parent):
    engine, sid, operation = continuation_parent
    with engine.begin() as conn:
        policy, encoded = _admit_parent(conn, sid, operation)
        reserve = _reserve(conn, sid, operation, encoded, 0)
    exact = encoded + " " * (policy.max_retained_payload_bytes - reserve - len(encoded.encode()))
    assert decode_continuation_snapshot(exact) == decode_continuation_snapshot(encoded)
    writes = []
    observer = _observe_dml(engine, writes)
    try:
        with pytest.raises(RuntimeError, match="observer control"), engine.begin() as conn:
            _initialize(conn, sid, operation, policy, exact)
            raise RuntimeError("observer control")
        assert writes
        writes.clear()
        before = _snapshot_rows(engine)
        with pytest.raises(ContinuationRetainedCapacityExceeded), engine.begin() as conn:
            _initialize(conn, sid, operation, policy, exact + " ")
        assert not writes and _snapshot_rows(engine) == before
        with engine.begin() as conn:
            _initialize(conn, sid, operation, policy, exact)
            assert (
                sum(size for _, size in _retained_payload_rows(conn, sid, operation).values()) + reserve
                == policy.max_retained_payload_bytes
            )
        assert writes
    finally:
        event.remove(engine, "before_cursor_execute", observer)


def test_off_head_chunks_charge_once_and_cannot_spend_terminal_reserve(continuation_parent):
    engine, sid, operation = continuation_parent
    with engine.begin() as conn:
        policy, encoded = _admit_parent(conn, sid, operation)
        _initialize(conn, sid, operation, policy, encoded)
        reserve = _reserve(conn, sid, operation, encoded, 0)
        used = sum(size for _, size in _retained_payload_rows(conn, sid, operation).values())
        payload = b"x" * (policy.max_retained_payload_bytes - reserve - used)
        assert payload
        put_chunk(conn, session_id=sid, operation_id=operation, key="off-head", payload=payload, codec="event.v2")
        before = _retained_payload_rows(conn, sid, operation)
        put_chunk(conn, session_id=sid, operation_id=operation, key="off-head", payload=payload, codec="event.v2")
        assert _retained_payload_rows(conn, sid, operation) == before
        with pytest.raises(ContinuationRetainedCapacityExceeded):
            put_chunk(conn, session_id=sid, operation_id=operation, key="another", payload=b"x", codec="event.v2")
        assert _retained_payload_rows(conn, sid, operation) == before
        with pytest.raises(AuditIntegrityError):
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key="another",
                payload=b"x",
                codec="event.v2",
                retained_payload_limit_bytes=policy.max_retained_payload_bytes + 1,
            )


@pytest.mark.parametrize("change", ("identity", "hash", "override"))
def test_initial_admission_refuses_substitution_before_dml(continuation_parent, change):
    engine, sid, operation = continuation_parent
    with engine.begin() as conn:
        policy, encoded = _admit_parent(conn, sid, operation)
    if change == "identity":
        root = decode_continuation_snapshot(encoded)
        encoded = encode_continuation_snapshot(
            root.model_copy(
                update={
                    "logical": root.logical.model_copy(update={"operation_id": "other"}),
                }
            )
        )
    writes = []
    observer = _observe_dml(engine, writes)
    try:
        before = _snapshot_rows(engine)
        with pytest.raises(AuditIntegrityError), engine.begin() as conn:
            initialize_continuation(
                conn,
                session_id=sid,
                operation_id=operation,
                checkpoint_id="initial-checkpoint",
                snapshot_json=encoded,
                action_json=policy.actions.session_sql.model_dump_json(),
                policy_sha256="f" * 64 if change == "hash" else policy.policy_hash,
                retained_payload_limit_bytes=policy.max_retained_payload_bytes + 1 if change == "override" else None,
            )
        assert not writes and _snapshot_rows(engine) == before
    finally:
        event.remove(engine, "before_cursor_execute", observer)


@pytest.mark.parametrize("prepared", (False, True))
@pytest.mark.parametrize("short_by", (0, 1))
def test_append_retains_actual_utf8_event_bytes(continuation_parent, prepared, short_by):
    engine, sid, operation = continuation_parent
    events = (ContinuationEvent("checkpoint", '{"text":"café"}'), ContinuationEvent("checkpoint", '{"text":"雪"}'))
    with engine.begin() as conn:
        policy, encoded = _admit_parent(conn, sid, operation)
        _initialize(conn, sid, operation, policy, encoded)
        current = load_snapshot(conn, session_id=sid, operation_id=operation)
        candidate = prepare_snapshot_append(current, snapshot_json=encoded, events=events, published_at=database_now(conn))
        used = sum(size for _, size in _retained_payload_rows(conn, sid, operation).values())
        appended_bytes = len(candidate.snapshot_json.encode()) + sum(len(item.payload_json.encode()) for item in events)
        reserve = _reserve(conn, sid, operation, candidate.snapshot_json, 2)
        ballast = policy.max_retained_payload_bytes - used - appended_bytes - reserve + short_by
        assert ballast > 0
        put_chunk(conn, session_id=sid, operation_id=operation, key="history", payload=b"x" * ballast, codec="event.v2")
    writes = []
    observer = _observe_dml(engine, writes)
    try:
        with pytest.raises(RuntimeError, match="observer control"), engine.begin() as conn:
            put_chunk(conn, session_id=sid, operation_id=operation, key="control", payload=b"x", codec="event.v2")
            raise RuntimeError("observer control")
        assert writes
        writes.clear()
        before_rows = _snapshot_rows(engine)
        with engine.begin() as conn:
            before = _retained_payload_rows(conn, sid, operation)
            kwargs = {
                "conn": conn,
                "session_id": sid,
                "operation_id": operation,
                "expected_checkpoint_id": current.checkpoint_id,
                "expected_cursor": current.cursor,
                "snapshot_json": encoded,
                "event_batch": events,
                "handoff": False,
                "prepared_append": candidate if prepared else None,
            }
            if short_by:
                with pytest.raises(ContinuationRetainedCapacityExceeded):
                    append_complete_snapshot(**kwargs)
                assert not writes and _retained_payload_rows(conn, sid, operation) == before
            else:
                result = append_complete_snapshot(**kwargs)
                after = _retained_payload_rows(conn, sid, operation)
                assert result.cursor == 2 and result.payload_json == candidate.snapshot_json
                assert sum(size for _, size in after.values()) - sum(size for _, size in before.values()) == appended_bytes
                assert sum(size for _, size in after.values()) + reserve == policy.max_retained_payload_bytes
                assert writes
        if short_by:
            assert _snapshot_rows(engine) == before_rows
    finally:
        event.remove(engine, "before_cursor_execute", observer)
