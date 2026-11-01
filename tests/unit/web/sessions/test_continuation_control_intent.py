"""Real paid PRIMARY sources for the dedicated control-intent SQL owner.

This storage prefix does not activate control-result messages or successors.
Only the SDK/guard transport is scripted; claim, quota, paid publication,
checkpoint transactions and cold ownership are the existing actual owners.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import delete, event, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_controls import (
    CONTINUATION_CALL_NOT_ALONE,
    CONTINUATION_CONTROL_RESULT,
    CONTINUATION_CONTROL_TOOL,
    ContinuationControlRefused,
)
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption, SessionOperationLease
from elspeth.web.sessions.composer_operations import (
    ComposerOperationCancelledDuringTurn,
    ComposerOperationFenceLost,
    ComposerOperationRunning,
)
from elspeth.web.sessions.continuation import ContinuationCASConflict, read_chunk
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import composition_states_table as states
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_budgets import OWNER_LEASE_SECONDS
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.coordination.test_continuation_p4_recovery_start import current_selected, new_owners
from tests.unit.web.sessions.test_continuation_primary_result import _assert_required_guard_admitted, initialized_primary_input

operation_store = _operation_store

_RAW_ARGUMENTS = ' {\n "notes" : "  Keep this same request.  "\n}\n'
_CALL_ID = "c" * 256
_TABLES = (states, messages, effects, physical, usage, jobs, segments, attempts, chunks, checkpoints, events)
_PAID_TABLES = (states, messages, effects, physical, usage, attempts)


def _rows(engine, tables=_TABLES):
    with engine.connect() as connection:
        return {table.name: tuple(connection.execute(select(table).order_by(*table.primary_key.columns)).all()) for table in tables}


@dataclass(slots=True)
class _PaidControlCase:
    store: Any
    running: ComposerOperationRunning
    selected: SelectedContinuationState
    inventory: CurrentContinuationEnvInventory
    primary: Any
    lease: SessionOperationLease
    sdk_calls: list[dict[str, Any]]
    raw_arguments: str


@asynccontextmanager
async def _paid_control_case(
    store, tmp_path, monkeypatch, *, raw_arguments=_RAW_ARGUMENTS, tool_name=CONTINUATION_CONTROL_TOOL, mixed=False
) -> AsyncIterator[_PaidControlCase]:
    runtime, work, requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    service, authority = runtime.materialization.service, store[2]
    running = work.running
    assert running is not None
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS, required_work=work.coordinator
    ).join()
    sdk_calls: list[dict[str, Any]] = []

    async def completion(**kwargs):
        _assert_required_guard_admitted(
            store[0], running.session_operation_context, requests, role="composer", user_prompt=runtime.bindings.message
        )
        assert not sdk_calls, "control storage or cold read re-entered the original paid PRIMARY"
        sdk_calls.append(kwargs)
        calls = [{"id": _CALL_ID, "type": "function", "function": {"name": tool_name, "arguments": raw_arguments}}]
        if mixed:
            calls.append(
                {"id": "sibling-mutation", "type": "function", "function": {"name": "set_source", "arguments": '{"plugin":"csv"}'}}
            )
        return ModelResponse(
            model="test/model",
            choices=[{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": calls}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        _messages, _tools, delivery, _delivery_input = service._prepare_primary_delivery(
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            turn_evidence=runtime.live.turn_evidence,
            state=runtime.live.state,
            failed_turn=runtime.live.failed_turn,
        )
        with prompt_guard_user_scope(runtime.bindings.message):
            primary = await run_continuation_primary(
                service=service,
                authority=authority,
                selected=runtime.source.selected,
                producers=runtime.materialization,
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                drained=runtime.drained,
                required_work=work,
                provider_owner=runtime.bindings.provider_owner,
                timeout=runtime.source.policy.actions.primary.total,
            )
        if delivery is not None:
            runtime.live.turn_evidence.confirm_delivery(delivery, runtime.live.recorder.llm_calls)
        work.coordinator.assert_completed()
        selected, inventory = await current_selected(store, running)
        assert type(selected.stack.frames[-1]) is ComposeFrame
        assert selected.stack.frames[-1].phase == "response_retained"
        assert primary.outcome.completion.tool_batch.calls[0].id == _CALL_ID
        assert primary.outcome.completion.tool_batch.calls[0].function.arguments == raw_arguments
        yield _PaidControlCase(store, running, selected, inventory, primary, lease, sdk_calls, raw_arguments)
    finally:
        await lease.close()


@pytest.mark.asyncio
async def test_actual_paid_control_intent_binds_exact_source_and_atomic_checkpoint(operation_store, tmp_path, monkeypatch):
    async with _paid_control_case(operation_store, tmp_path, monkeypatch) as case:
        engine, _repository, authority, _sessions, sid = operation_store
        paid_before = _rows(engine, _PAID_TABLES)
        retained = authority.retain_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory)
        source = retained.source
        audit = case.primary.outcome.audit_call
        assert audit is not None
        frame = case.selected.stack.frames[-1]
        fence = case.running.session_operation_context.fence
        assert source.schema_version == "composer.control-intent.v1"
        assert source.previous_checkpoint_id == case.selected.checkpoint_id and source.previous_cursor == case.selected.stack.cursor
        assert (source.sequence, source.generation) == (case.running.claim.segment_sequence, case.running.claim.ownership_generation)
        assert (source.creator_operation_id, source.creator_operation_epoch) == (fence.operation_id, fence.operation_epoch)
        assert source.provider_effect_key == case.primary.effect_key
        assert source.provider_input_sha256 == frame.provider_input_sha256
        assert source.provider_call_id == audit.call_id and source.response == case.primary.response.reference
        assert source.tool_call_id == _CALL_ID
        assert source.arguments_sha256 == sha256(case.raw_arguments.encode("utf-8")).hexdigest()
        assert source.arguments_sha256 != sha256(json.dumps(json.loads(case.raw_arguments)).encode("utf-8")).hexdigest()
        assert "raw_arguments" not in source.model_dump()
        assert retained.result == CONTINUATION_CONTROL_RESULT
        assert retained.snapshot.cursor == case.selected.stack.cursor + 1
        selected, inventory = await current_selected(operation_store, case.running)
        assert selected.checkpoint_id == retained.snapshot.checkpoint_id
        assert selected.stack.model_copy(update={"cursor": case.selected.stack.cursor}) == case.selected.stack
        assert selected.chunks == case.selected.chunks
        assert authority.read_continuation_control_intent(case.running, selected=selected, inventory=inventory) == retained
        with engine.connect() as connection:
            row = connection.execute(
                select(events).where(
                    events.c.session_id == str(sid),
                    events.c.operation_id == case.running.claim.operation_id,
                    events.c.sequence == retained.snapshot.cursor,
                )
            ).one()
            payload = json.loads(
                read_chunk(
                    connection,
                    session_id=str(sid),
                    operation_id=case.running.claim.operation_id,
                    key=row.payload_chunk_key,
                )
            )
        assert row.tag == "checkpoint"
        assert payload == {
            "owner": "composer_control_intent",
            "source": source.model_dump(mode="json"),
            "result": CONTINUATION_CONTROL_RESULT,
        }
        assert _rows(engine, _PAID_TABLES) == paid_before and len(case.sdk_calls) == 1


@pytest.mark.asyncio
async def test_control_intent_replay_returns_committed_receipt_without_new_rows_or_paid_work(operation_store, tmp_path, monkeypatch):
    async with _paid_control_case(operation_store, tmp_path, monkeypatch) as case:
        engine, _repository, authority, *_ = operation_store
        original = authority.retain_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory)
        selected, inventory = await current_selected(operation_store, case.running)
        before = _rows(engine)
        replay = authority.retain_continuation_control_intent(case.running, selected=selected, inventory=inventory)
        assert replay == original
        assert authority.read_continuation_control_intent(case.running, selected=selected, inventory=inventory) == original
        assert _rows(engine) == before and len(case.sdk_calls) == 1


@pytest.mark.asyncio
async def test_paid_response_without_control_intent_does_not_synthesize_a_receipt(operation_store, tmp_path, monkeypatch):
    async with _paid_control_case(operation_store, tmp_path, monkeypatch) as case:
        before = _rows(operation_store[0])
        assert operation_store[2].read_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory) is None
        assert _rows(operation_store[0]) == before and len(case.sdk_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("malformed", "mixed", "ordinary"))
async def test_refused_or_noncontrol_paid_batch_creates_no_accepted_intent(operation_store, tmp_path, monkeypatch, kind):
    arguments = '{"notes":' if kind == "malformed" else _RAW_ARGUMENTS
    tool_name = "get_pipeline_state" if kind == "ordinary" else CONTINUATION_CONTROL_TOOL
    async with _paid_control_case(
        operation_store,
        tmp_path,
        monkeypatch,
        raw_arguments=arguments,
        tool_name=tool_name,
        mixed=kind == "mixed",
    ) as case:
        before = _rows(operation_store[0])
        if kind == "ordinary":
            with pytest.raises(AuditIntegrityError):
                operation_store[2].retain_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory)
        else:
            with pytest.raises(ContinuationControlRefused) as refused:
                operation_store[2].retain_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory)
            assert refused.value.error_code == (CONTINUATION_CALL_NOT_ALONE if kind == "mixed" else "argument_error")
        assert _rows(operation_store[0]) == before and len(case.sdk_calls) == 1


@pytest.mark.asyncio
async def test_durable_stop_refuses_control_intent_without_checkpoint_or_paid_replay(operation_store, tmp_path, monkeypatch):
    async with _paid_control_case(operation_store, tmp_path, monkeypatch) as case:
        authority = operation_store[2]
        authority.request_cancel(session_id=case.running.claim.session_id, operation_id=case.running.claim.operation_id)
        before = _rows(operation_store[0])
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            authority.retain_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory)
        assert _rows(operation_store[0]) == before and len(case.sdk_calls) == 1


@pytest.mark.asyncio
async def test_retained_control_intent_remains_readable_data_after_stop_without_reacceptance(operation_store, tmp_path, monkeypatch):
    async with _paid_control_case(operation_store, tmp_path, monkeypatch) as case:
        authority = operation_store[2]
        retained = authority.retain_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory)
        selected, inventory = await current_selected(operation_store, case.running)
        authority.request_cancel(session_id=case.running.claim.session_id, operation_id=case.running.claim.operation_id)
        before = _rows(operation_store[0])
        assert authority.read_continuation_control_intent(case.running, selected=selected, inventory=inventory) == retained
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            authority.retain_continuation_control_intent(case.running, selected=selected, inventory=inventory)
        assert _rows(operation_store[0]) == before and len(case.sdk_calls) == 1


@pytest.mark.asyncio
async def test_cold_control_intent_reads_original_paid_source_and_refuses_stale_owner(operation_store, tmp_path, monkeypatch):
    async with _paid_control_case(operation_store, tmp_path, monkeypatch) as case:
        retained = operation_store[2].retain_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory)
        await case.lease.close()
        fresh = new_owners(operation_store)
        engine, repository, authority, _sessions, sid = fresh
        second = None
        try:
            assert (
                repository.recover_composer_segment(
                    session_id=sid,
                    operation_id=case.running.claim.operation_id,
                    expected_generation=case.running.claim.ownership_generation,
                    actor_user_id="alice",
                    auth_provider_type="local",
                )
                is True
            )
            (claim,) = authority.claim_segment(limit=1)
            context = repository.start_composer_segment(
                claim,
                owner_instance_id=authority._owner_instance_id,
                lease_seconds=OWNER_LEASE_SECONDS,
                auth_provider_type="local",
            )
            second = ComposerOperationRunning(claim, context)
            assert claim.segment_sequence == case.running.claim.segment_sequence
            assert claim.ownership_generation == case.running.claim.ownership_generation + 1
            assert context.fence.operation_epoch > case.running.session_operation_context.fence.operation_epoch
            selected, inventory = await current_selected(fresh, second)
            before = _rows(engine)
            original = authority.read_continuation_control_intent(second, selected=selected, inventory=inventory)
            assert original == retained
            assert original.source.sequence == case.running.claim.segment_sequence
            assert original.source.generation == case.running.claim.ownership_generation
            assert selected.stack.frames == case.selected.stack.frames
            with pytest.raises((SessionOperationFenceLost, ComposerOperationFenceLost, ContinuationCASConflict, AuditIntegrityError)):
                operation_store[2].read_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory)
            assert _rows(engine) == before and len(case.sdk_calls) == 1
        finally:
            if second is not None:
                repository.release(second.session_operation_context)


@pytest.mark.asyncio
async def test_control_intent_missing_paid_effect_refuses_without_writing_or_replaying(operation_store, tmp_path, monkeypatch):
    async with _paid_control_case(operation_store, tmp_path, monkeypatch) as case:
        engine, _repository, authority, *_ = operation_store
        # Deliberate integrity opponent after the real complete paid positive.
        assert case.primary.outcome.audit_call is not None and len(case.sdk_calls) == 1
        with engine.begin() as connection:
            connection.execute(delete(effects).where(effects.c.effect_key == case.primary.effect_key))
        before = _rows(engine)
        with pytest.raises(AuditIntegrityError):
            authority.retain_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory)
        assert _rows(engine) == before and len(case.sdk_calls) == 1


@pytest.mark.asyncio
async def test_control_intent_reader_rechecks_actual_physical_creator_after_positive(operation_store, tmp_path, monkeypatch):
    async with _paid_control_case(operation_store, tmp_path, monkeypatch) as case:
        engine, _repository, authority, *_ = operation_store
        retained = authority.retain_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory)
        selected, inventory = await current_selected(operation_store, case.running)
        assert authority.read_continuation_control_intent(case.running, selected=selected, inventory=inventory) == retained
        with engine.begin() as connection:
            connection.execute(
                update(physical)
                .where(physical.c.attempt_id == retained.source.provider_call_id)
                .values(lease_token=case.running.session_operation_context.fence.lease_token + "changed")
            )
        before = _rows(engine)
        with pytest.raises(AuditIntegrityError):
            authority.read_continuation_control_intent(case.running, selected=selected, inventory=inventory)
        assert _rows(engine) == before and len(case.sdk_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("table", (checkpoints, events), ids=("checkpoint", "event"))
async def test_control_intent_checkpoint_failure_rolls_back_all_rows(operation_store, tmp_path, monkeypatch, table):
    async with _paid_control_case(operation_store, tmp_path, monkeypatch) as case:
        engine, _repository, authority, *_ = operation_store
        before = _rows(engine)
        failure = RuntimeError("actual control checkpoint write failure")
        reached = []

        def fail_insert(_conn, _cursor, _statement, _parameters, execution, _executemany):
            compiled = execution.compiled
            if compiled is not None and compiled.isinsert and compiled.statement.table.name == table.name:
                reached.append(table.name)
                raise failure

        event.listen(engine, "after_cursor_execute", fail_insert)
        try:
            with pytest.raises(RuntimeError) as caught:
                authority.retain_continuation_control_intent(case.running, selected=case.selected, inventory=case.inventory)
        finally:
            event.remove(engine, "after_cursor_execute", fail_insert)
        assert caught.value is failure and reached == [table.name]
        assert _rows(engine) == before and len(case.sdk_calls) == 1
