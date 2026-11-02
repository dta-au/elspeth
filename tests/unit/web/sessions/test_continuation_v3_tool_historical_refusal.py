"""Historical V3 custody proofs, not a live paid V3 admission path.

The fixture installs an explicit historical paid source under an originally
admitted V3 policy. Production PRIMARY readers must prove its full descriptor,
audit, physical attempt and ledger. No provenance reader is replaced.
"""

from __future__ import annotations

import time
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import event, func, insert, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation import EffectFrontierEntry, ToolBatchFrame
from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot
from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_chunk, encode_coupled_batch_chunk
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_response, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import ContinuationEnvSelection, CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_data import append_primary_call_to_evidence
from elspeth.web.composer.continuation_phase_transitions import refuse_v3_tool_before_dispatch
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV3
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.provider_audit_source import provider_attempt_audit_drafts
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import _usage_entry_id, token_usage_entry_from_llm_call_envelope
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, load_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.continuation_primary import read_committed_primary_response_on_connection
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection
from elspeth.web.sessions.continuation_tool_no_execution import (
    read_v3_tool_no_execution_refusal_on_connection,
    refuse_v3_tool_no_execution_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions import test_continuation_p4_owner as p4_cases
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input, primary_input
from tests.unit.web.sessions.test_continuation_v3_paid_tail_preflight import _store_with_policy

operation_store = test_composer_operation_authority.operation_store


async def historical_tool_cut(operation_store, tmp_path, monkeypatch, *, initialized=False, tool_name="set_metadata"):
    """Construct historical DATA; never dispatch or relax live V3 preflight."""
    if tool_name not in ("set_metadata", "get_pipeline_state"):
        raise ValueError("Historical tool fixture requires an explicit supported tool shape")
    store, policy = _store_with_policy(operation_store, monkeypatch, version="v3")
    initial = None
    if initialized:
        monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
        initial, work, _requests = await initialized_primary_input(store, tmp_path, monkeypatch)
        assert type(initial.source.policy) is ContinuationAdmissionPolicyV3
        assert initial.source.policy == policy
        assert work.running is not None
        selected = initial.source.selected
        kwargs = {
            "initial_runtime": initial,
            "service": initial.materialization.service,
            "authority": store[2],
            "selected": selected,
            "required_work": work,
            "provider_owner": initial.bindings.provider_owner,
            "inventory": CurrentContinuationEnvInventory(
                work.running,
                ContinuationEnvSelection(selected.checkpoint_id, selected.stack.cursor, encode_continuation_snapshot(selected.stack)),
                initial.env_ref_names,
            ),
        }
    else:
        kwargs, _produced, _recorder, _requests = await primary_input(store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    before = kwargs["selected"]
    batch, accumulator, completion, _prepared = p4_cases.completed_cut(
        running,
        before.stack,
        initial_state=None if initial is None else initial.live.state,
        current_state_id=None if initial is None else initial.live.current_state_id,
    )
    # Choose the original provider response before any audit, response bytes,
    # accounting or source root is retained. Never rewrite paid custody later.
    if tool_name == "get_pipeline_state":
        original_call = completion.tool_batch.calls[0]
        call = replace(original_call, function=replace(original_call.function, name=tool_name, arguments="{}"))
        completion = replace(completion, tool_batch=replace(completion.tool_batch, calls=(call,)))
        batch = replace(batch, decoded_args_by_call_id={call.id: {}})
    batch = replace(batch, phase="call_ready", next_tool_index=0, tool_outcomes=[], private_authorings_by_call={})
    batch_chunk = encode_coupled_batch_chunk(batch=batch, accumulator=accumulator, calls=completion.tool_batch.calls)
    wire = [{"role": "user", "content": "Historical tool source"}]
    descriptor = {
        "schema": "composer.provider-input.v1",
        "messages": wire,
        "tools_present": False,
        "tools": None,
        "options": {"model": "owned-model"},
    }
    input_hash = stable_hash(descriptor)
    sid, operation = str(store[4]), running.claim.operation_id
    with locked_session_transaction(store[0], sid) as conn:
        started = database_now(conn)
        audit = replace(
            build_llm_call_record(
                model_requested="owned-model",
                messages=wire,
                tools=None,
                status=ComposerLLMCallStatus.SUCCESS,
                started_at=started,
                started_ns=time.monotonic_ns(),
                temperature=None,
                seed=None,
                response_metadata=completion.provider_metadata,
                cost_response_accountable=True,
                private_request=capture_provider_context(messages=wire, tools=None),
            ),
            call_id=str(uuid4()),
        )
        assert audit.call_id is not None
        envelope = llm_call_audit_envelope(audit)
        entry = token_usage_entry_from_llm_call_envelope(envelope)
        assert entry is not None
        entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=audit.call_id)
        conn.execute(
            insert(usage).values(
                entry_id=entry_id,
                identity_id="alice",
                session_id=sid,
                source="composer",
                run_id=None,
                model=entry.model,
                prompt_tokens=entry.prompt_tokens,
                completion_tokens=entry.completion_tokens,
                cached_prompt_tokens=entry.cached_prompt_tokens,
                reasoning_tokens=entry.reasoning_tokens,
                recorded_at=entry.recorded_at,
            )
        )
        fence = running.session_operation_context.fence
        conn.execute(
            insert(physical).values(
                attempt_id=audit.call_id,
                identity_id="alice",
                session_id=sid,
                source="composer",
                run_id=None,
                started_at=audit.started_at,
                settled_at=audit.finished_at,
                operation_id=fence.operation_id,
                operation_epoch=fence.operation_epoch,
                lease_token=fence.lease_token,
                ledger_entry_id=entry_id,
            )
        )
        sequence = (
            conn.execute(select(func.coalesce(func.max(messages.c.sequence_no), 0)).where(messages.c.session_id == sid)).scalar_one() + 1
        )
        for index, draft in enumerate(provider_attempt_audit_drafts(audit, running.session_operation_context)):
            conn.execute(
                insert(messages).values(
                    id=str(uuid4()),
                    session_id=sid,
                    role=draft.role,
                    content=draft.content,
                    tool_calls=deep_thaw(draft.tool_calls),
                    sequence_no=sequence + index,
                    writer_principal="compose_loop",
                    created_at=audit.finished_at,
                )
            )
        response = encode_continuation_response(completion, audit_call=audit)
        compose = before.stack.frames[0]
        previous = decode_recorder_snapshot(next(chunk for chunk in before.chunks if chunk.reference == compose.audit_buffer))
        evidence = decode_turn_evidence(
            next(chunk for chunk in before.chunks if chunk.reference == compose.evidence),
            env_ref_names=kwargs["inventory"].names,
            source_chunks=before.chunks,
        )
        extended = append_primary_call_to_evidence(
            evidence,
            call=audit,
            recorder=replace(previous, llm_calls=(*previous.llm_calls, audit)),
            env_ref_names=kwargs["inventory"].names,
        )
        evidence_chunk = encode_turn_evidence(extended, env_ref_names=kwargs["inventory"].names)
        for chunk in (response, batch_chunk, extended.recorder_chunk, evidence_chunk):
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode(),
                codec=chunk.reference.codec,
            )
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=f"provider-input:{input_hash}",
            payload=canonical_json(descriptor).encode(),
            codec="composer.provider-input.v1",
        )
        conn.execute(
            insert(effects).values(
                session_id=sid,
                operation_id=operation,
                effect_key="provider:0:0",
                input_sha256=input_hash,
                owner="provider",
                disposition="committed",
                receipt_key=audit.call_id,
                result_chunk_key=private_chunk_key(response),
                creator_sequence=running.claim.segment_sequence,
                creator_generation=running.claim.ownership_generation,
                provider_attempt_id=audit.call_id,
                physical_sequence=running.claim.segment_sequence,
                physical_generation=running.claim.ownership_generation,
                requested_model=audit.model_requested,
                response_finished_at=audit.finished_at,
            )
        )
        compose = before.stack.frames[0].model_copy(
            update={
                "phase": "response_retained",
                "pending_response": response.reference,
                "provider_effect_key": "provider:0:0",
                "provider_input_sha256": input_hash,
                "audit_buffer": extended.recorder_chunk.reference,
                "evidence": evidence_chunk.reference,
            }
        )
        root = before.stack.model_copy(
            update={
                # The live fixture lets the actual phase owner initialize and
                # capture its shared batch/evidence graph from the paid source.
                "frames": (compose,)
                if initialized
                else (
                    compose,
                    ToolBatchFrame(
                        phase="tool_ready",
                        response=response.reference,
                        accumulator=batch_chunk.reference,
                        next_tool_index=0,
                        settled_effect_ids=(),
                    ),
                ),
                "effect_frontier": (EffectFrontierEntry(effect_id="provider:0:0", disposition="committed", receipt_id=audit.call_id),),
            }
        )
        head = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=before.checkpoint_id,
            expected_cursor=before.stack.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(
                ContinuationEvent(
                    "response_retained", canonical_json({"effect_key": "provider:0:0", "response_digest": response.reference.digest})
                ),
            ),
            handoff=False,
        )
        root = decode_continuation_snapshot(head.payload_json)
        selected = SelectedContinuationState(
            head.checkpoint_id, root, load_snapshot_private_data_on_connection(conn, session_id=sid, operation_id=operation, selected=head)
        )
        assert read_committed_primary_response_on_connection(conn, running, selected=selected, reference=response.reference) == response
        assert read_continuation_cost_snapshot_on_connection(conn, root) is not None
    inventory = CurrentContinuationEnvInventory(
        running, ContinuationEnvSelection(head.checkpoint_id, root.cursor, head.payload_json), kwargs["inventory"].names
    )
    return store, policy, running, selected, inventory, batch, accumulator, completion, kwargs


def _rows(engine, sid, operation):
    with engine.connect() as conn:
        return {
            table.name: tuple(conn.execute(select(table).where(table.c.session_id == sid).order_by(*table.primary_key.columns)).all())
            for table in (jobs, chunks, effects, physical, usage, messages, checkpoints, events, segments, attempts)
        }


def _cold(conn, store, running, inventory, receipt):
    return read_v3_tool_no_execution_refusal_on_connection(
        conn,
        session_id=store[4],
        operation_id=running.claim.operation_id,
        effect_key=receipt.effect_key,
        env_ref_names=inventory.names,
    )


@pytest.mark.asyncio
async def test_historical_v3_original_paid_source_retains_tool_refusal_without_new_payment(operation_store, tmp_path, monkeypatch):
    store, _policy, running, selected, inventory, *_ = await historical_tool_cut(operation_store, tmp_path, monkeypatch)
    sid, operation = str(store[4]), running.claim.operation_id
    try:
        before = _rows(store[0], sid, operation)
        receipt = refuse_v3_tool_before_dispatch(store[2], running, selected=selected, inventory=inventory)
        with store[0].connect() as conn:
            assert _cold(conn, store, running, inventory, receipt) == receipt
        after = _rows(store[0], sid, operation)
        assert after[physical.name] == before[physical.name]
        assert after[usage.name] == before[usage.name]
        assert after[messages.name] == before[messages.name]
        assert after[effects.name][0] == before[effects.name][0]
        assert receipt.source_checkpoint_id == selected.checkpoint_id
        assert receipt.source_cursor == selected.stack.cursor
    finally:
        store[1].release(running.session_operation_context)


class _ProbeRolledBack(Exception):
    """The real committing owner was exercised only inside a rolled-back probe."""


@pytest.mark.asyncio
@pytest.mark.parametrize("short_by", (0, 1))
async def test_historical_v3_refusal_admits_full_tail_before_dml(operation_store, tmp_path, monkeypatch, short_by):
    from elspeth.web.sessions import continuation

    store, policy, running, selected, inventory, *_ = await historical_tool_cut(operation_store, tmp_path, monkeypatch)
    sid, operation = str(store[4]), running.claim.operation_id
    peaks = []
    original = continuation.admit_retained_payload_chunks

    def observe_admission(conn, *, session_id, operation_id, prospective, retained_payload_limit_bytes):
        # Observe the real central admission; do not replace its decision or
        # predict the receipt/transition/terminal serialization independently.
        existing = dict(
            conn.execute(
                select(chunks.c.chunk_key, chunks.c.byte_length).where(
                    chunks.c.session_id == session_id, chunks.c.operation_id == operation_id
                )
            ).all()
        )
        proposed = {candidate.key: candidate for candidate in prospective}
        peaks.append(sum(existing.values()) + sum(len(candidate.payload) for key, candidate in proposed.items() if key not in existing))
        return original(
            conn,
            session_id=session_id,
            operation_id=operation_id,
            prospective=prospective,
            retained_payload_limit_bytes=retained_payload_limit_bytes,
        )

    monkeypatch.setattr(continuation, "admit_retained_payload_chunks", observe_admission)
    try:
        before = _rows(store[0], sid, operation)
        with pytest.raises(_ProbeRolledBack), locked_session_transaction(store[0], sid) as conn:
            receipt = refuse_v3_tool_no_execution_on_connection(conn, running, selected=selected, inventory=inventory)
            assert _cold(conn, store, running, inventory, receipt) == receipt
            raise _ProbeRolledBack
        assert _rows(store[0], sid, operation) == before
        assert len(peaks) >= 3
        padding = policy.max_retained_payload_bytes - max(peaks) + short_by
        assert padding > 0
        # History counts toward the unchanged originally admitted allowance.
        # Every extra chunk remains within the existing 1 MiB document limit.
        with store[0].begin() as conn:
            index = 0
            while padding:
                length = min(padding, 1_048_576)
                payload = b"0" if length == 1 else b'"' + b"x" * (length - 2) + b'"'
                conn.execute(
                    insert(chunks).values(
                        session_id=sid,
                        operation_id=operation,
                        chunk_key=f"historical-padding:{index}",
                        codec="event.v2",
                        payload=payload,
                        sha256=sha256(payload).hexdigest(),
                        byte_length=length,
                    )
                )
                padding -= length
                index += 1
        before = _rows(store[0], sid, operation)
        dml = []

        def observe_dml(conn, cursor, statement, parameters, context, executemany):
            if context.isinsert or context.isupdate or context.isdelete:
                dml.append(statement)

        event.listen(store[0], "before_cursor_execute", observe_dml)
        try:
            # Control the SQL observer with an actual write, rolled back, then
            # a SELECT which must not be reported as mutation.
            with store[0].connect() as conn:
                transaction = conn.begin()
                conn.execute(update(chunks).where(chunks.c.session_id == sid).values(byte_length=chunks.c.byte_length))
                transaction.rollback()
                assert dml
                dml.clear()
                conn.execute(select(chunks.c.chunk_key).where(chunks.c.session_id == sid)).all()
                assert not dml
            peaks.clear()
            if short_by:
                with pytest.raises(ContinuationRetainedCapacityExceeded):
                    refuse_v3_tool_before_dispatch(store[2], running, selected=selected, inventory=inventory)
                assert not dml
                assert _rows(store[0], sid, operation) == before
                assert max(peaks) == policy.max_retained_payload_bytes + 1
                with store[0].connect() as conn:
                    assert load_snapshot(conn, session_id=sid, operation_id=operation).checkpoint_id == selected.checkpoint_id
            else:
                receipt = refuse_v3_tool_before_dispatch(store[2], running, selected=selected, inventory=inventory)
                assert dml
                assert max(peaks) == policy.max_retained_payload_bytes
                with store[0].connect() as conn:
                    assert _cold(conn, store, running, inventory, receipt) == receipt
                after = _rows(store[0], sid, operation)
                assert after[physical.name] == before[physical.name]
                assert after[usage.name] == before[usage.name]
                assert after[messages.name] == before[messages.name]
        finally:
            event.remove(store[0], "before_cursor_execute", observe_dml)
    finally:
        store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("phase", "next_index"))
async def test_cold_tool_refusal_rejects_coherent_original_accumulator_substitution(operation_store, tmp_path, monkeypatch, changed):
    store, _policy, running, selected, inventory, batch, accumulator, completion, _context = await historical_tool_cut(
        operation_store, tmp_path, monkeypatch
    )
    sid, operation = str(store[4]), running.claim.operation_id
    try:
        receipt = refuse_v3_tool_before_dispatch(store[2], running, selected=selected, inventory=inventory)
        with store[0].connect() as conn:
            assert _cold(conn, store, running, inventory, receipt) == receipt
        if changed == "phase":
            altered = replace(batch, phase="dispatch_ready", stop_issued_batch=True)
        else:
            completed, accumulator, completion, _prepared = p4_cases.completed_cut(running, selected.stack)
            altered = replace(completed, phase="call_ready")
        replacement = encode_coupled_batch_chunk(batch=altered, accumulator=accumulator, calls=completion.tool_batch.calls)
        decoded = decode_coupled_batch_chunk(replacement)
        assert decoded.batch.phase == altered.phase
        assert decoded.batch.next_tool_index == altered.next_tool_index
        with store[0].begin() as conn:
            conn.execute(
                insert(chunks).values(
                    session_id=sid,
                    operation_id=operation,
                    chunk_key=private_chunk_key(replacement),
                    codec=replacement.reference.codec,
                    payload=replacement.canonical.encode(),
                    sha256=replacement.reference.digest,
                    byte_length=replacement.reference.byte_length,
                )
            )
            # Coherently replace the accumulator in all three snapshots. Hash,
            # codec, source/terminal root relation and PRIMARY proof stay valid.
            snapshots = conn.execute(
                select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.codec == "snapshot.v2")
            ).all()
            for row in snapshots:
                root = decode_continuation_snapshot(row.payload.decode())
                if len(root.frames) != 2 or not isinstance(root.frames[-1], ToolBatchFrame):
                    continue
                altered_root = root.model_copy(
                    update={"frames": (*root.frames[:-1], root.frames[-1].model_copy(update={"accumulator": replacement.reference}))}
                )
                payload = encode_continuation_snapshot(altered_root).encode()
                conn.execute(
                    update(chunks)
                    .where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == row.chunk_key)
                    .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                )
        with store[0].connect() as conn, pytest.raises(AuditIntegrityError, match="changed its original unissued accumulator"):
            _cold(conn, store, running, inventory, receipt)
    finally:
        store[1].release(running.session_operation_context)
