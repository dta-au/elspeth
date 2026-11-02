"""A real paid semantic decision crossing into one source-bound PRIMARY call."""

import json

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_message_shelf import decode_message_shelf
from elspeth.web.composer.continuation_primary import read_continuation_primary, run_continuation_primary
from elspeth.web.composer.continuation_prompt_producers import load_current_message_producers
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationEvent, append_complete_snapshot
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.continuation_primary import (
    read_committed_primary_response_data_on_connection,
    read_original_committed_primary_response_data_on_connection,
)
from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection
from elspeth.web.sessions.continuation_semantic_advisor import (
    consume_semantic_advisor_primary_on_connection,
    install_semantic_advisor_decision_on_connection,
    retain_semantic_advisor_outcome_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.web.composer.test_continuation_semantic_advisor import _answer, _reply
from tests.unit.web.sessions.test_continuation_primary_result import publish_actual
from tests.unit.web.sessions.test_continuation_semantic_advisor import _begin, _invoke, _semantic_case
from tests.unit.web.sessions.test_continuation_semantic_advisor import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
async def test_historical_paid_primary_reader_survives_new_head_but_refuses_corrupt_original_ledger(operation_store, tmp_path, monkeypatch):
    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    sid = str(operation_store[4])
    try:
        assert result is not None and len(sdk) == 1
        original_root = decode_continuation_snapshot(result.snapshot.payload_json)
        with locked_session_transaction(operation_store[0], sid) as conn:
            selected = SelectedContinuationState(
                result.snapshot.checkpoint_id,
                original_root,
                load_snapshot_private_data_on_connection(
                    conn, session_id=sid, operation_id=running.claim.operation_id, selected=result.snapshot
                ),
            )
            assert (
                read_committed_primary_response_data_on_connection(
                    conn,
                    session_id=operation_store[4],
                    operation_id=running.claim.operation_id,
                    selected=selected,
                    reference=result.response.reference,
                )
                == result.response
            )
            advanced = append_complete_snapshot(
                conn,
                session_id=sid,
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=result.snapshot.checkpoint_id,
                expected_cursor=result.snapshot.cursor,
                snapshot_json=encode_continuation_snapshot(original_root),
                event_batch=(ContinuationEvent("checkpoint", canonical_json({"fixture": "later historical PRIMARY reader"})),),
                handoff=False,
            )
            assert advanced.checkpoint_id != result.snapshot.checkpoint_id
            with pytest.raises(ContinuationCASConflict):
                read_committed_primary_response_data_on_connection(
                    conn,
                    session_id=operation_store[4],
                    operation_id=running.claim.operation_id,
                    selected=selected,
                    reference=result.response.reference,
                )
            historical = read_original_committed_primary_response_data_on_connection(
                conn,
                session_id=operation_store[4],
                operation_id=running.claim.operation_id,
                source_checkpoint_id=result.snapshot.checkpoint_id,
                source_cursor=result.snapshot.cursor,
                response=result.response.reference,
            )
            assert historical == result.response
            savepoint = conn.begin_nested()
            conn.execute(update(usage).where(usage.c.session_id == sid).values(prompt_tokens=999))
            with pytest.raises(AuditIntegrityError, match="original complete token ledger"):
                read_original_committed_primary_response_data_on_connection(
                    conn,
                    session_id=operation_store[4],
                    operation_id=running.claim.operation_id,
                    source_checkpoint_id=result.snapshot.checkpoint_id,
                    source_cursor=result.snapshot.cursor,
                    response=result.response.reference,
                )
            savepoint.rollback()
            assert (
                read_original_committed_primary_response_data_on_connection(
                    conn,
                    session_id=operation_store[4],
                    operation_id=running.claim.operation_id,
                    source_checkpoint_id=result.snapshot.checkpoint_id,
                    source_cursor=result.snapshot.cursor,
                    response=result.response.reference,
                )
                == result.response
            )
        assert len(sdk) == 1
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_cost_source_still_refuses_corrupt_family_zero_response_after_real_paid_primary(operation_store, tmp_path, monkeypatch):
    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    try:
        assert result is not None and len(sdk) == 1
        stack = decode_continuation_snapshot(result.snapshot.payload_json)
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            original = read_continuation_cost_snapshot_on_connection(conn, stack)
            assert original is not None and original.physical_calls == 1
            savepoint = conn.begin_nested()
            conn.execute(update(effects).where(effects.c.effect_key == result.effect_key).values(disposition="dispatched"))
            with pytest.raises(AuditIntegrityError, match="Continuation cost source lost its committed PRIMARY response"):
                read_continuation_cost_snapshot_on_connection(conn, stack)
            savepoint.rollback()
            assert read_continuation_cost_snapshot_on_connection(conn, stack) == original
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_paid_primary_prescription_reaches_one_physical_request_and_cold_reader(operation_store, tmp_path, monkeypatch):
    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        sid = str(operation_store[4])
        running = case["running"]
        authority = case["runtime"].materialization.authority
        service = case["runtime"].materialization.service
        observed_calls = []

        async def advisor_completion(**kwargs):
            observed_calls.append(kwargs)
            return _reply(json.dumps(_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", advisor_completion)
        issuer, prepared, dispatched = _begin(operation_store, case)
        observed = await _invoke(case, issuer)
        assert observed.result.decision is not None
        action = observed.result.decision.prescribed_action
        assert action is not None and action.kind == "primary_turn"
        with locked_session_transaction(operation_store[0], sid) as conn:
            retained = retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=observed, recorder=case["recorder"], issuer=issuer
            )
        with locked_session_transaction(operation_store[0], sid) as conn:
            installed = install_semantic_advisor_decision_on_connection(
                conn, running, selected=retained.snapshot, effect_key=prepared.binding.effect_key
            )
        with locked_session_transaction(operation_store[0], sid) as conn:
            consumed = consume_semantic_advisor_primary_on_connection(
                conn, running, selected=installed, effect_key=prepared.binding.effect_key
            )
        root = decode_continuation_snapshot(consumed.payload_json)
        assert len(root.frames) == 1 and type(root.frames[0]) is ComposeFrame and root.frames[0].phase == "action_ready"
        assert root.active_action_json == canonical_json(action.model_dump(mode="json"))
        assert root.active_action_source is not None and root.active_action_source.effect_key == prepared.binding.effect_key

        selected = await load_selected_continuation(authority=authority, running=running, selected=consumed, required_work=case["work"])
        inventory = await materialize_continuation_env_inventory(
            advisor=service._advisor_checkpoint, authority=authority, selected=selected, required_work=case["work"]
        )
        frame = selected.stack.frames[0]
        assert type(frame) is ComposeFrame
        shelf = decode_message_shelf(next(chunk for chunk in selected.chunks if chunk.reference == frame.transcript))
        produced = await load_current_message_producers(
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            required_work=case["work"],
            retained_shelf=shelf,
        )
        assert produced.messages is not None
        tools = (
            [] if frame.tools is None else json.loads(next(chunk.canonical for chunk in selected.chunks if chunk.reference == frame.tools))
        )

        async def primary_completion(**kwargs):
            observed_calls.append(kwargs)
            return _reply("Actual prescribed PRIMARY reply")

        monkeypatch.setattr(litellm, "acompletion", primary_completion)
        with prompt_guard_user_scope(case["runtime"].bindings.message):
            result = await run_continuation_primary(
                service=service,
                authority=authority,
                selected=selected,
                producers=produced.producers,
                messages=list(produced.messages),
                tools=tools,
                recorder=case["recorder"],
                drained=case["runtime"].drained,
                required_work=case["work"],
                provider_owner=case["provider"],
                timeout=5,
            )
        case["work"].coordinator.assert_completed()
        assert len(observed_calls) == 2
        assert observed_calls[1]["messages"][-1] == {"role": "assistant", "content": action.instruction}
        committed = decode_continuation_snapshot(result.snapshot.payload_json)
        assert committed.active_action_json is None and committed.active_action_source is None
        with operation_store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.effect_key == result.effect_key)).one()
            ledger = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
            charged = read_continuation_cost_snapshot_on_connection(conn, committed)
        assert effect.action_json == root.active_action_json and effect.disposition == "committed"
        assert len(ledger) == 2 and result.outcome.audit_call.call_id is not None
        assert charged is not None
        assert {charge.call_id for charge in charged.charges} == {
            observed.result.audit_call.call_id,
            result.outcome.audit_call.call_id,
        }
        cold_selected = await load_selected_continuation(
            authority=authority, running=running, selected=result.snapshot, required_work=case["work"]
        )
        cold = await read_continuation_primary(authority=authority, selected=cold_selected, required_work=case["work"])
        case["work"].coordinator.assert_completed()
        assert cold.outcome.audit_call.call_id == result.outcome.audit_call.call_id
        assert len(observed_calls) == 2
