"""Real current message reads share session exclusion and keep the final guard."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from elspeth.contracts.prompt_guard import detach_prompt_guard_evidence
from elspeth.web.composer import continuation_current_evidence as bridge
from elspeth.web.composer import continuation_prompt_producers as producers
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.locking import sqlite_process_session_lock
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from tests.unit.web.coordination.test_continuation_p4_recovery_start import recovery_rows
from tests.unit.web.sessions import test_continuation_prompt_producers as producer_cases

operation_store = producer_cases.operation_store


def _try_session_lock(engine, session_id):
    with sqlite_process_session_lock(engine, session_id, blocking=False) as acquired:
        return acquired


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", (False, True))
async def test_actual_producer_reads_hold_same_session_exclusion(operation_store, monkeypatch, initial):
    arguments, _evidence, _recorder, _snapshot, _iteration, _actors = await producer_cases.producer_case(operation_store)
    running = arguments["inventory"].running
    engine, session_id = operation_store[0], str(operation_store[4])
    try:
        normal = await producers.load_current_message_producers(**arguments, initial=initial)
        # Calibrate the actual nonblocking lock probe outside a held scope.
        assert await asyncio.to_thread(_try_session_lock, engine, session_id) is True
        original = producers._read_current_inputs
        observed = []

        def observe_read(conn, **kwargs):
            # A different thread must be excluded before the original full SQL
            # reader runs. The observer neither replaces it nor supplies DATA.
            with ThreadPoolExecutor(max_workers=1) as executor:
                acquired = executor.submit(_try_session_lock, engine, session_id).result(timeout=5)
            assert acquired is False
            value = original(conn, **kwargs)
            observed.append(value)
            return value

        monkeypatch.setattr(producers, "_read_current_inputs", observe_read)
        before = recovery_rows(engine)
        actual = await producers.load_current_message_producers(**arguments, initial=initial)
        assert observed and recovery_rows(engine) == before
        assert actual.producers.inputs.bootstrap == normal.producers.inputs.bootstrap
        assert actual.producers.inputs.producer_inputs == normal.producers.inputs.producer_inputs
        assert actual.producers.inputs.evidence_messages_json == normal.producers.inputs.evidence_messages_json
        if initial:
            assert actual.messages is not None and normal.messages is not None
            assert detach_prompt_guard_evidence(actual.messages).messages == detach_prompt_guard_evidence(normal.messages).messages
        else:
            assert actual.messages is normal.messages is None
    finally:
        arguments["required_work"].coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_bridge_reuses_actual_read_and_keeps_fresh_final_refusal(operation_store, monkeypatch):
    arguments, _evidence, _recorder, snapshot, _iteration, _actors = await producer_cases.producer_case(operation_store)
    running = arguments["inventory"].running
    engine = operation_store[0]
    try:
        production = await producers.load_current_message_producers(**arguments)
        normal = await bridge.restore_current_turn_evidence(**arguments, materialization=production.producers)
        assert normal.messages == [{"role": "user", "content": producer_cases.REQUEST}]
        assert normal.evidence.request_id == snapshot.request_id

        def forbidden_duplicate(*args, **kwargs):
            raise AssertionError("bridge repeated the complete evidence facade after its actual current-input read")

        monkeypatch.setattr(arguments["authority"], "read_continuation_turn_evidence", forbidden_duplicate)
        before = recovery_rows(engine)
        restored = await bridge.restore_current_turn_evidence(**arguments, materialization=production.producers)
        assert restored.messages == normal.messages and recovery_rows(engine) == before
        assert restored.evidence.request_id == normal.evidence.request_id
        actual_restore = bridge._restore_current_evidence
        after_stop = []

        def stop_after_actual_restore(*args, **kwargs):
            result = actual_restore(*args, **kwargs)
            # Fixture setup is after the real reconstruction and before the
            # final guard; retain a complete snapshot after that exact write.
            with engine.begin() as conn:
                changed = conn.execute(
                    update(jobs)
                    .where(jobs.c.session_id == str(operation_store[4]), jobs.c.operation_id == running.claim.operation_id)
                    .values(cancel_requested_at=database_now(conn))
                )
                assert changed.rowcount == 1
            after_stop.append(recovery_rows(engine))
            return result

        monkeypatch.setattr(bridge, "_restore_current_evidence", stop_after_actual_restore)
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await bridge.restore_current_turn_evidence(**arguments, materialization=production.producers)
        assert len(after_stop) == 1 and recovery_rows(engine) == after_stop[0]
    finally:
        arguments["required_work"].coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)
