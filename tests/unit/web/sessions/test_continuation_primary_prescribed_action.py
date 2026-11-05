"""A real paid semantic decision crossing into one source-bound PRIMARY call."""

import json
from contextlib import asynccontextmanager

import litellm
import pytest
from sqlalchemy import delete, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_message_shelf import decode_message_shelf
from elspeth.web.composer.continuation_primary import read_continuation_primary, run_continuation_primary
from elspeth.web.composer.continuation_progress import decode_continuation_frontier, decode_prescribed_continuation_action
from elspeth.web.composer.continuation_prompt_producers import load_current_message_producers
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.continuation_semantic_advisor_phase import run_continuation_semantic_advisor
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.sessions import continuation_primary as primary_sql
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    append_complete_snapshot,
    load_snapshot,
    read_chunk,
)
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.continuation_primary import (
    PrimaryNoSdkRefusalReceipt,
    PrimaryProviderResult,
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
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.web.composer.test_continuation_semantic_advisor import _answer, _reply
from tests.unit.web.sessions.test_continuation_primary_result import publish_actual
from tests.unit.web.sessions.test_continuation_semantic_advisor import _begin, _invoke, _semantic_case
from tests.unit.web.sessions.test_continuation_semantic_advisor import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_semantic_v4_live import _phases, _v4_store

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


@asynccontextmanager
async def _v4_paid_primary_prescription(store, tmp_path, monkeypatch):
    """Real paid/action SQL primitives, with each original stage allowance bound."""
    store = _v4_store(store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch, timed_read=True) as case:
        runtime, running, work = case["runtime"], case["running"], case["work"]
        phases = _phases(case)
        calls = []

        async def semantic_completion(**kwargs):
            calls.append(("semantic", kwargs))
            return _reply(json.dumps(_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", semantic_completion)
        deadline = work.coordinator.sql_deadline_owner
        assert deadline is not None
        semantic_clock = ContinuationActionClock(runtime.source.policy.actions.advisor)
        deadline.begin_action(semantic_clock.policy, started_at=semantic_clock.started_monotonic)
        phases.bind_action_clock(semantic_clock)
        assert await run_continuation_semantic_advisor(phases, runtime.source.selected) is None
        paid = phases.latest_snapshot
        assert paid is not None and [role for role, _request in calls] == ["semantic"]
        advisor = decode_continuation_snapshot(paid.payload_json).frames[-1]
        assert type(advisor) is AdvisorFrame and advisor.provider_effect_key is not None
        sid = str(running.claim.session_id)
        with locked_session_transaction(store[0], sid) as conn:
            installed = install_semantic_advisor_decision_on_connection(
                conn, running, selected=paid, effect_key=advisor.provider_effect_key
            )
            consumed = consume_semantic_advisor_primary_on_connection(
                conn, running, selected=installed, effect_key=advisor.provider_effect_key
            )
            semantic_effect = conn.execute(select(effects).where(effects.c.effect_key == advisor.provider_effect_key)).one()
            semantic_hold = conn.execute(select(holds).where(holds.c.effect_key == advisor.provider_effect_key)).one()
            assert semantic_effect.disposition == "committed" and semantic_hold.state == "consumed"
        service, authority = runtime.materialization.service, runtime.materialization.authority
        selected = await load_selected_continuation(authority=authority, running=running, selected=consumed, required_work=work)
        root = selected.stack
        assert root.active_action_json is not None and root.active_action_source is not None
        assert root.active_action_source.effect_key == advisor.provider_effect_key
        action = decode_prescribed_continuation_action(root.active_action_json)
        assert action.kind == "primary_turn"
        frame = root.frames[0]
        assert len(root.frames) == 1 and type(frame) is ComposeFrame and frame.phase == "action_ready"
        inventory = await materialize_continuation_env_inventory(
            advisor=service._advisor_checkpoint, authority=authority, selected=selected, required_work=work
        )
        shelf = decode_message_shelf(next(chunk for chunk in selected.chunks if chunk.reference == frame.transcript))
        produced = await load_current_message_producers(
            service=service, authority=authority, selected=selected, inventory=inventory, required_work=work, retained_shelf=shelf
        )
        assert produced.messages is not None
        tools = (
            [] if frame.tools is None else json.loads(next(chunk.canonical for chunk in selected.chunks if chunk.reference == frame.tools))
        )

        async def primary_completion(**kwargs):
            calls.append(("primary", kwargs))
            assert kwargs["messages"][-1] == {"role": "assistant", "content": action.instruction}
            return _reply("Actual V4 prescribed PRIMARY reply")

        monkeypatch.setattr(litellm, "acompletion", primary_completion)

        async def invoke():
            primary_clock = ContinuationActionClock(runtime.source.policy.actions.primary)
            assert primary_clock is not semantic_clock
            deadline.begin_action(primary_clock.policy, started_at=primary_clock.started_monotonic)
            phases.bind_action_clock(primary_clock)
            with prompt_guard_user_scope(runtime.bindings.message):
                return await run_continuation_primary(
                    service=service,
                    authority=authority,
                    selected=selected,
                    producers=produced.producers,
                    messages=list(produced.messages),
                    tools=tools,
                    recorder=case["recorder"],
                    drained=runtime.drained,
                    required_work=work,
                    provider_owner=case["provider"],
                    timeout=runtime.source.policy.actions.primary.total,
                )

        yield {**case, "store": store, "invoke": invoke, "calls": calls, "selected": selected, "semantic_effect": semantic_effect}


def _prescribed_receipts(conn, running, snapshot, effect_key):
    root = decode_continuation_snapshot(snapshot.payload_json)
    ref = root.semantic_frontier
    assert ref is not None
    canonical = read_chunk(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        key=f"value:{ref.codec}:{ref.digest}",
    ).decode("utf-8")
    frontier = decode_continuation_frontier(canonical)
    return tuple(item.disposition for item in frontier.action_receipts if item.owner_receipt_id == effect_key), canonical


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("complete", "before_hold", "after_hold"))
async def test_v4_paid_prescription_preserves_actual_action_history_and_original_terminal(operation_store, tmp_path, monkeypatch, outcome):
    async with _v4_paid_primary_prescription(operation_store, tmp_path, monkeypatch) as case:
        store, running = case["store"], case["running"]
        authority = case["runtime"].materialization.authority
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        admissions, prepared = [], []
        actual_admit = primary_sql.admit_v4_primary_provider_on_connection
        actual_dispatch = ProviderCallCustody.require_cost_dispatch
        actual_prepare = type(authority).prepare_provider_action

        def observe_admission(conn, *args, **kwargs):
            admission = actual_admit(conn, *args, **kwargs)
            assert load_snapshot(conn, session_id=sid, operation_id=operation) == admission.selected
            history, _canonical = _prescribed_receipts(conn, running, admission.selected, admission.hold.effect_key)
            assert history == ("prepared", "dispatched")
            tail = admission.no_sdk_tail.action_chunk
            assert tail is not None
            predicted = decode_continuation_frontier(tail.canonical)
            assert tuple(item.disposition for item in predicted.action_receipts if item.owner_receipt_id == admission.hold.effect_key) == (
                "prepared",
                "dispatched",
                "refused",
            )
            admissions.append(admission)
            return admission

        def refuse_after_hold(custody):
            if custody.continuation_primary_v4_admission is not None:
                assert admissions == [custody.continuation_primary_v4_admission]
                if outcome == "after_hold":
                    raise TimeoutError("controlled prescribed PRIMARY refusal after original H")
            return actual_dispatch(custody)

        def prepare_then_refuse(owner, *args, **kwargs):
            value = actual_prepare(owner, *args, **kwargs)
            assert owner is authority
            prepared.append(value)
            if outcome == "before_hold":
                raise TimeoutError("controlled prescribed PRIMARY refusal before original H")
            return value

        monkeypatch.setattr(primary_sql, "admit_v4_primary_provider_on_connection", observe_admission)
        monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", refuse_after_hold)
        monkeypatch.setattr(type(authority), "prepare_provider_action", prepare_then_refuse)
        result = await case["invoke"]()
        assert len(prepared) == 1
        assert len(admissions) == (0 if outcome == "before_hold" else 1)
        assert [role for role, _request in case["calls"]] == (["semantic", "primary"] if outcome == "complete" else ["semantic"])
        assert type(result) is (PrimaryProviderResult if outcome == "complete" else PrimaryNoSdkRefusalReceipt)
        with locked_session_transaction(store[0], sid) as conn:
            current = load_snapshot(conn, session_id=sid, operation_id=operation)
            root = decode_continuation_snapshot(current.payload_json)
            assert root.active_action_json is root.active_action_source is None
            history, canonical = _prescribed_receipts(conn, running, current, result.effect_key)
            assert history == (
                ("prepared", "dispatched", "committed")
                if outcome == "complete"
                else ("prepared", "refused")
                if outcome == "before_hold"
                else ("prepared", "dispatched", "refused")
            )
            effect = conn.execute(select(effects).where(effects.c.effect_key == result.effect_key)).one()
            assert effect.action_json == case["selected"].stack.active_action_json
            assert effect.disposition == ("committed" if outcome == "complete" else "refused")
            assert (
                conn.execute(select(effects).where(effects.c.effect_key == case["semantic_effect"].effect_key)).one()
                == case["semantic_effect"]
            )
            current_holds = conn.execute(select(holds).where(holds.c.effect_key == result.effect_key)).all()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
            if outcome == "before_hold":
                assert current_holds == [] and effect.provider_attempt_id is None and len(entries) == 1
            else:
                (hold,) = current_holds
                attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
                entry = conn.execute(select(usage).where(usage.c.entry_id == attempt.ledger_entry_id)).one()
                assert hold.state == "consumed" and attempt.settled_at is not None and len(entries) == 2
                assert effect.provider_attempt_id == admissions[0].attempt.attempt_id == attempt.attempt_id
                if outcome == "after_hold":
                    assert entry.prompt_tokens == entry.completion_tokens == entry.cached_prompt_tokens == entry.reasoning_tokens == 0
                    assert canonical == admissions[0].no_sdk_tail.action_chunk.canonical
            if outcome != "complete":
                assert (
                    primary_sql.read_primary_no_sdk_refusal_on_connection(
                        conn, session_id=running.claim.session_id, operation_id=operation, effect_key=result.effect_key
                    )
                    == result
                )
        if outcome == "complete":
            cold_selected = await load_selected_continuation(
                authority=authority, running=running, selected=result.snapshot, required_work=case["work"]
            )
            cold = await read_continuation_primary(authority=authority, selected=cold_selected, required_work=case["work"])
            assert cold.outcome.audit_call.call_id == result.outcome.audit_call.call_id
        case["work"].coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("missing", "payload"))
async def test_v4_prescribed_admission_refuses_real_persisted_frontier_damage_before_original_writer(
    operation_store, tmp_path, monkeypatch, corruption
):
    async with _v4_paid_primary_prescription(operation_store, tmp_path, monkeypatch) as case:
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        authority = case["runtime"].materialization.authority
        actual_prepare = type(authority).prepare_provider_action
        actual_read = primary_sql.read_chunk
        checked, prepared_sources = [], []

        def retain_original_prepared_source(owner, *args, **kwargs):
            prepared = actual_prepare(owner, *args, **kwargs)
            assert owner is authority and prepared.binding.running is running
            ref = decode_continuation_snapshot(prepared.snapshot.payload_json).semantic_frontier
            assert ref is not None
            prepared_sources.append((prepared, ref))
            return prepared

        def damage_real_frontier(conn, *, session_id, operation_id, key):
            if checked or not prepared_sources or (session_id, operation_id) != (sid, operation):
                return actual_read(conn, session_id=session_id, operation_id=operation_id, key=key)
            ((prepared, ref),) = prepared_sources
            if key != f"value:{ref.codec}:{ref.digest}":
                return actual_read(conn, session_id=session_id, operation_id=operation_id, key=key)
            selected = load_snapshot(conn, session_id=sid, operation_id=operation)
            assert selected == prepared.snapshot
            assert ref == decode_continuation_snapshot(selected.payload_json).semantic_frontier
            effect = conn.execute(
                select(effects).where(
                    effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == prepared.binding.effect_key
                )
            ).one()
            assert effect.disposition == "prepared" and effect.action_json == case["selected"].stack.active_action_json
            original = actual_read(conn, session_id=sid, operation_id=operation, key=key)
            damaged_payload = b"!" + original[1:]
            assert len(damaged_payload) == len(original) and damaged_payload != original
            criteria = (chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == key)
            savepoint = conn.begin_nested()
            try:
                damaged = conn.execute(
                    delete(chunks).where(*criteria)
                    if corruption == "missing"
                    else update(chunks).where(*criteria).values(payload=damaged_payload)
                )
                assert damaged.rowcount == 1
                expected_error = (
                    "continuation chunk digest or binding mismatch"
                    if corruption == "missing"
                    else "continuation chunk exceeded its original bound or lost its SQL binding"
                )
                with pytest.raises(AuditIntegrityError, match=f"^{expected_error}$"):
                    actual_read(conn, session_id=sid, operation_id=operation, key=key)
                assert [role for role, _request in case["calls"]] == ["semantic"]
                checked.append(corruption)
            finally:
                savepoint.rollback()
            restored = actual_read(conn, session_id=sid, operation_id=operation, key=key)
            assert restored == original
            assert load_snapshot(conn, session_id=sid, operation_id=operation) == selected
            return restored

        monkeypatch.setattr(type(authority), "prepare_provider_action", retain_original_prepared_source)
        monkeypatch.setattr(primary_sql, "read_chunk", damage_real_frontier)
        result = await case["invoke"]()
        assert checked == [corruption] and type(result) is PrimaryProviderResult
        assert [role for role, _request in case["calls"]] == ["semantic", "primary"]
        with case["store"][0].connect() as conn:
            history, _canonical = _prescribed_receipts(conn, running, result.snapshot, result.effect_key)
        assert history == ("prepared", "dispatched", "committed")
