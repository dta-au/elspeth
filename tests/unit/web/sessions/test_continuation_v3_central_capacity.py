"""Central V3 admission charges the original root and every later chunk key."""

from __future__ import annotations

import pytest
from sqlalchemy import event, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ContinuationProviderCostPolicyV2
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV3, decode_continuation_policy
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    PreparedContinuationAppend,
    append_complete_snapshot,
    initialize_continuation,
    load_snapshot,
    put_chunk,
)
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_frames import initial_stack
from tests.unit.web.sessions.test_continuation_schema import _retained_payload_rows, _snapshot_rows

pytest_plugins = ("tests.unit.web.sessions.test_continuation_schema",)


def _root(sid, operation_id, limit):
    old = initial_stack()
    v2 = continuation_policy_from_settings(continuation_test_settings())
    policy = ContinuationAdmissionPolicyV3(
        segment=v2.segment,
        actions=v2.actions,
        provider_cost=v2.provider_cost,
        max_retained_payload_bytes=limit,
    )
    return old.model_copy(
        update={
            "logical": old.logical.model_copy(update={"session_id": sid, "operation_id": operation_id}),
            "mutation_ceiling": old.mutation_ceiling.model_copy(update={"session_id": sid}),
            "provider_cost_policy": ContinuationProviderCostPolicyV2(
                applicability="continuation_v2",
                lifecycle_version=2,
                accounting_owner="physical_audit_and_token_ledger",
                admission_policy_hash=policy.policy_hash,
            ),
            "policy_hash": policy.policy_hash,
            "admission_policy_json": policy.model_dump_json(),
        }
    )


def _sized_root(sid, operation_id, *, extra_bytes, events=()):
    limit = 1
    for _ in range(12):
        root = _root(sid, operation_id, limit)
        initial = encode_continuation_snapshot(root)
        final = encode_continuation_snapshot(root.model_copy(update={"cursor": len(events)})) if events else ""
        terminal_event = ContinuationEvent(
            "settled",
            canonical_json({"version": 1, "sequence": 1, "generation": 1, "status": "failed", "failure_code": "http_error"}),
        )
        terminal = encode_continuation_snapshot(root.model_copy(update={"cursor": len(events) + 1}))
        sized = len(initial.encode("utf-8")) + len(final.encode("utf-8")) + len(terminal.encode("utf-8"))
        sized += sum(len(item.payload_json.encode("utf-8")) for item in events)
        sized += len(terminal_event.payload_json.encode("utf-8")) + extra_bytes
        if sized == limit:
            return root, initial
        limit = sized
    raise AssertionError("test policy/UTF-8 size did not stabilize")


def _limit(root):
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    assert isinstance(policy, ContinuationAdmissionPolicyV3)
    return policy.max_retained_payload_bytes


def _set_v3_contract(conn, sid, operation_id, root):
    conn.execute(
        update(jobs)
        .where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)
        .values(
            continuation_policy_version="composer.continuation.policy.v3",
            continuation_policy_sha256=root.policy_hash,
            continuation_retained_payload_limit_bytes=_limit(root),
        )
    )


def _initialize(connection, sid, operation_id, root_json, *, policy_hash, override=None):
    initialize_continuation(
        connection,
        session_id=sid,
        operation_id=operation_id,
        checkpoint_id="initial-checkpoint",
        snapshot_json=root_json,
        action_json=continuation_policy_from_settings(continuation_test_settings()).actions.session_sql.model_dump_json(),
        policy_sha256=policy_hash,
        retained_payload_limit_bytes=override,
    )


def _used(connection, sid, operation_id):
    return sum(size for _codec, size in _retained_payload_rows(connection, sid, operation_id).values())


def _reserved_failed_tail_bytes(root, cursor):
    terminal_event = ContinuationEvent(
        "settled",
        canonical_json({"version": 1, "sequence": 1, "generation": 1, "status": "failed", "failure_code": "http_error"}),
    )
    terminal = encode_continuation_snapshot(root.model_copy(update={"cursor": cursor + 1}))
    return len(terminal_event.payload_json.encode("utf-8")) + len(terminal.encode("utf-8"))


def _observe_dml(engine, writes):
    def observed(_connection, _cursor, statement, _parameters, context, _executemany):
        if context.isinsert or context.isupdate or context.isdelete:
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", observed)
    return observed


def test_v3_initial_root_exact_fit_and_one_byte_short_before_dml(continuation_parent):
    engine, sid, operation_id = continuation_parent
    root, encoded = _sized_root(sid, operation_id, extra_bytes=0)
    with engine.begin() as conn:
        _set_v3_contract(conn, sid, operation_id, root)
    writes: list[str] = []
    observed = _observe_dml(engine, writes)
    try:
        with pytest.raises(RuntimeError, match="positive control"), engine.begin() as conn:
            _initialize(conn, sid, operation_id, encoded, policy_hash=root.policy_hash)
            raise RuntimeError("positive control")
        assert writes, "DML observer must detect an actual INSERT"
        writes.clear()
        before = _snapshot_rows(engine)
        short_json = encoded + " "
        with pytest.raises(ContinuationRetainedCapacityExceeded), engine.begin() as conn:
            _initialize(conn, sid, operation_id, short_json, policy_hash=root.policy_hash)
        assert writes == [] and _snapshot_rows(engine) == before

        with engine.begin() as conn:
            _initialize(conn, sid, operation_id, encoded, policy_hash=root.policy_hash)
            assert _used(conn, sid, operation_id) == len(encoded.encode("utf-8"))
        assert writes
    finally:
        event.remove(engine, "before_cursor_execute", observed)


@pytest.mark.parametrize("change", ("identity", "hash", "override"))
def test_v3_initial_root_rejects_foreign_identity_hash_or_override_before_dml(continuation_parent, change):
    engine, sid, operation_id = continuation_parent
    root, encoded = _sized_root(sid, operation_id, extra_bytes=0)
    with engine.begin() as conn:
        _set_v3_contract(conn, sid, operation_id, root)
    supplied_hash, supplied_override = root.policy_hash, None
    if change == "identity":
        encoded = encode_continuation_snapshot(
            root.model_copy(update={"logical": root.logical.model_copy(update={"operation_id": "other"})})
        )
    elif change == "hash":
        supplied_hash = "f" * 64
    else:
        supplied_override = _limit(root) + 1
    writes: list[str] = []
    observed = _observe_dml(engine, writes)
    try:
        before = _snapshot_rows(engine)
        with pytest.raises(AuditIntegrityError), engine.begin() as conn:
            _initialize(conn, sid, operation_id, encoded, policy_hash=supplied_hash, override=supplied_override)
        assert writes == [] and _snapshot_rows(engine) == before
    finally:
        event.remove(engine, "before_cursor_execute", observed)


def test_v3_direct_chunk_uses_policy_without_override_and_counts_off_head_keys(continuation_parent):
    engine, sid, operation_id = continuation_parent
    root, encoded = _sized_root(sid, operation_id, extra_bytes=2)
    with engine.begin() as conn:
        _set_v3_contract(conn, sid, operation_id, root)
        _initialize(conn, sid, operation_id, encoded, policy_hash=root.policy_hash)
        put_chunk(conn, session_id=sid, operation_id=operation_id, key="off-head", payload=b"{}", codec="event.v2")
        assert _used(conn, sid, operation_id) + _reserved_failed_tail_bytes(root, 0) == _limit(root)
        put_chunk(conn, session_id=sid, operation_id=operation_id, key="off-head", payload=b"{}", codec="event.v2")
        assert _used(conn, sid, operation_id) + _reserved_failed_tail_bytes(root, 0) == _limit(root)
        with pytest.raises(ContinuationRetainedCapacityExceeded):
            put_chunk(conn, session_id=sid, operation_id=operation_id, key="another", payload=b"{}", codec="event.v2")
        with pytest.raises(AuditIntegrityError, match="immutable admission policy"):
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation_id,
                key="another",
                payload=b"{}",
                codec="event.v2",
                retained_payload_limit_bytes=_limit(root) + 10,
            )


def _prepared_append(*, sid, operation_id, current, final_json, events, at):
    original = (
        sid,
        operation_id,
        current.checkpoint_id,
        current.cursor,
        final_json,
        events,
        "prepared-checkpoint",
        at,
        False,
        current.payload_json,
    )
    return PreparedContinuationAppend(*original, original)


@pytest.mark.parametrize("prepared", (False, True))
@pytest.mark.parametrize("extra_bytes", (0, -1))
def test_v3_append_charges_final_root_and_all_raw_utf8_events_before_dml(continuation_parent, prepared, extra_bytes):
    engine, sid, operation_id = continuation_parent
    events = (
        ContinuationEvent("checkpoint", '{"text":"café"}'),
        ContinuationEvent("checkpoint", '{"text":"雪"}'),
    )
    root, encoded = _sized_root(sid, operation_id, extra_bytes=extra_bytes, events=events)
    with engine.begin() as conn:
        _set_v3_contract(conn, sid, operation_id, root)
        _initialize(conn, sid, operation_id, encoded, policy_hash=root.policy_hash)
    writes: list[str] = []
    observed = _observe_dml(engine, writes)
    try:
        with pytest.raises(RuntimeError, match="positive control"), engine.begin() as conn:
            put_chunk(conn, session_id=sid, operation_id=operation_id, key="dml-positive", payload=b"{}", codec="event.v2")
            raise RuntimeError("positive control")
        assert writes, "DML observer must detect an actual INSERT"
        writes.clear()
        before = _snapshot_rows(engine)
        with engine.begin() as conn:
            current = load_snapshot(conn, session_id=sid, operation_id=operation_id)
            final_json = encode_continuation_snapshot(decode_continuation_snapshot(current.payload_json).model_copy(update={"cursor": 2}))
            prepared_value = (
                _prepared_append(
                    sid=sid,
                    operation_id=operation_id,
                    current=current,
                    final_json=final_json,
                    events=events,
                    at=database_now(conn),
                )
                if prepared
                else None
            )
            if extra_bytes < 0:
                with pytest.raises(ContinuationRetainedCapacityExceeded):
                    append_complete_snapshot(
                        conn,
                        session_id=sid,
                        operation_id=operation_id,
                        expected_checkpoint_id=current.checkpoint_id,
                        expected_cursor=current.cursor,
                        snapshot_json=current.payload_json,
                        event_batch=events,
                        handoff=False,
                        prepared_append=prepared_value,
                    )
                assert writes == []
            else:
                retained = append_complete_snapshot(
                    conn,
                    session_id=sid,
                    operation_id=operation_id,
                    expected_checkpoint_id=current.checkpoint_id,
                    expected_cursor=current.cursor,
                    snapshot_json=current.payload_json,
                    event_batch=events,
                    handoff=False,
                    prepared_append=prepared_value,
                )
                assert retained.payload_json == final_json and retained.cursor == 2
                assert _used(conn, sid, operation_id) + _reserved_failed_tail_bytes(root, 2) == _limit(root)
        if extra_bytes < 0:
            assert _snapshot_rows(engine) == before
        else:
            assert writes
    finally:
        event.remove(engine, "before_cursor_execute", observed)
