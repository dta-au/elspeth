"""An admitted V3 root must leave space for a durable failed terminal."""

from __future__ import annotations

import pytest
from sqlalchemy import event, select

from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    _v3_terminal_append_scope,
    append_complete_snapshot,
    load_snapshot,
    put_chunk,
    read_v3_retained_tail_refusal_on_connection,
    refuse_v3_retained_tail_before_paid,
)
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_v3_central_capacity as central
from tests.unit.web.sessions.test_continuation_primary_result import primary_input
from tests.unit.web.sessions.test_continuation_v3_paid_tail_preflight import _store_with_policy

pytest_plugins = ("tests.unit.web.sessions.test_continuation_schema",)
operation_store = test_composer_operation_authority.operation_store


def _minimal_failed_tail() -> ContinuationEvent:
    # This is only the settled event; a real PRIMARY/TITLE/tool refusal also
    # retains its receipt and effect event, so the lower bound is conservative.
    return ContinuationEvent(
        "settled", canonical_json({"version": 1, "sequence": 1, "generation": 1, "status": "failed", "failure_code": "http_error"})
    )


def test_v3_initial_root_rejects_one_byte_short_of_even_minimal_failed_tail_before_dml(continuation_parent):
    engine, sid, operation_id = continuation_parent
    terminal_event = _minimal_failed_tail()
    fit, _initial = central._sized_root(sid, operation_id, extra_bytes=0)
    short = central._root(sid, operation_id, central._limit(fit) - 1)
    short_json = encode_continuation_snapshot(short)
    required = (
        len(short_json.encode("utf-8"))
        + len(encode_continuation_snapshot(short.model_copy(update={"cursor": 1})).encode("utf-8"))
        + len(terminal_event.payload_json.encode("utf-8"))
    )
    assert required > central._limit(short)
    writes: list[str] = []
    observed = central._observe_dml(engine, writes)
    try:
        with pytest.raises(ContinuationRetainedCapacityExceeded), engine.begin() as conn:
            central._set_v3_contract(conn, sid, operation_id, short)
            writes.clear()
            central._initialize(conn, sid, operation_id, short_json, policy_hash=short.policy_hash)
        assert writes == []
    finally:
        event.remove(engine, "before_cursor_execute", observed)


def test_v3_initial_root_with_exact_minimal_terminal_space_can_retain_both(continuation_parent):
    engine, sid, operation_id = continuation_parent
    terminal_event = _minimal_failed_tail()
    fit, initial = central._sized_root(sid, operation_id, extra_bytes=0)
    with engine.begin() as conn:
        central._set_v3_contract(conn, sid, operation_id, fit)
        central._initialize(conn, sid, operation_id, initial, policy_hash=fit.policy_hash)
        stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
        with _v3_terminal_append_scope(conn):
            final = append_complete_snapshot(
                conn,
                session_id=sid,
                operation_id=operation_id,
                expected_checkpoint_id=stored.checkpoint_id,
                expected_cursor=stored.cursor,
                snapshot_json=initial,
                event_batch=(terminal_event,),
                handoff=False,
            )
        assert final.cursor == 1
        assert central._used(conn, sid, operation_id) == central._limit(fit)


def test_v3_direct_chunk_and_append_cannot_spend_protected_terminal_space(continuation_parent):
    engine, sid, operation_id = continuation_parent
    fit, initial = central._sized_root(sid, operation_id, extra_bytes=0)
    with engine.begin() as conn:
        central._set_v3_contract(conn, sid, operation_id, fit)
        central._initialize(conn, sid, operation_id, initial, policy_hash=fit.policy_hash)
    writes: list[str] = []
    observed = central._observe_dml(engine, writes)
    try:
        with pytest.raises(ContinuationRetainedCapacityExceeded), engine.begin() as conn:
            put_chunk(conn, session_id=sid, operation_id=operation_id, key="bypass", payload=b"x", codec="event.v2")
        assert writes == []
        with pytest.raises(ContinuationRetainedCapacityExceeded), engine.begin() as conn:
            stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
            append_complete_snapshot(
                conn,
                session_id=sid,
                operation_id=operation_id,
                expected_checkpoint_id=stored.checkpoint_id,
                expected_cursor=stored.cursor,
                snapshot_json=initial,
                event_batch=(ContinuationEvent("checkpoint", "{}"),),
                handoff=False,
            )
        assert writes == []
    finally:
        event.remove(engine, "before_cursor_execute", observed)


@pytest.mark.asyncio
async def test_v3_generic_unpaid_refusal_has_strict_cold_no_effect_no_physical_proof(operation_store, tmp_path, monkeypatch):
    store, _policy = _store_with_policy(operation_store, monkeypatch, version="v3")
    kwargs, _produced, _recorder, _requests = await primary_input(store, tmp_path, monkeypatch)
    running, selected, inventory = kwargs["inventory"].running, kwargs["selected"], kwargs["inventory"]
    receipt = refuse_v3_retained_tail_before_paid(store[2], running, selected=selected, inventory=inventory)
    assert receipt.reason == "retained_tail_unbounded"
    assert receipt.source_checkpoint_id == selected.checkpoint_id and receipt.source_cursor == selected.stack.cursor
    assert receipt.terminal.cursor == selected.stack.cursor + 1
    with store[0].connect() as conn:
        assert read_v3_retained_tail_refusal_on_connection(conn, session_id=store[4], operation_id=running.claim.operation_id) == receipt
        assert not conn.execute(
            select(effects.c.effect_key).where(effects.c.session_id == str(store[4]), effects.c.operation_id == running.claim.operation_id)
        ).all()
        assert not conn.execute(
            select(attempts.c.attempt_id).where(
                attempts.c.session_id == str(store[4]), attempts.c.operation_id == running.claim.operation_id
            )
        ).all()
