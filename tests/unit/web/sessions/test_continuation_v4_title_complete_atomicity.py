"""Source-real V4 TITLE complete result uses one credited SQL transaction."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _AdmittedToolBatch
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_title_values import capture_title_provider_result, decode_title_provider_result
from elspeth.web.composer.llm_response_parsing import admit_v4_provider_result
from elspeth.web.composer.provider_audit_source import provider_attempt_audit_drafts
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.provider_result_admission import V4ProviderSdkReturn, diagnostic_provider_metadata
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation import ContinuationEvent, prepare_snapshot_append
from elspeth.web.sessions.continuation_title_paid_overflow_terminal import (
    read_v4_title_paid_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_title_provider import (
    CurrentTitleNames,
    _descriptor,
    _verified_v4_title_stored_source_on_connection,
    admit_v4_title_provider_on_connection,
)
from elspeth.web.sessions.continuation_v4_title_complete_tail import (
    build_v4_title_complete_batch,
    require_v4_title_complete_batch_matches_capture,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_title_values import title_result_inputs
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_v3_title_paid_tail_preflight import _historical_prepared_title

operation_store = operations.operation_store


def _issued_title_completion(attempt_id: str, *, reasoning: str = "Retained reasoning") -> _AdmittedLLMCompletion:
    response = ModelResponse(
        id="request-original",
        model="model-actual",
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Pipeline title",
                    "tool_calls": [],
                    "reasoning_content": reasoning,
                    "reasoning_details": [{"text": "safe detail"}],
                },
            }
        ],
        usage={"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18},
    )
    choice = response.choices[0]
    admitted = admit_v4_provider_result(
        response,
        choice=choice,
        message=choice.message,
        sdk_return=V4ProviderSdkReturn(attempt_id, id(response)),
    )
    return _AdmittedLLMCompletion(
        admitted.message,
        _AdmittedToolBatch(admitted.tool_calls, frozenset(call.id for call in admitted.tool_calls)),
        admitted.metadata,
    )


@pytest.mark.asyncio
async def test_v4_title_complete_result_commits_received_settled_ledger_and_hold_once(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, _composer_service, _work, prepared, _guard_requests = await _historical_prepared_title(store, policy, tmp_path, monkeypatch)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(store[0], sid) as conn:
        admission = admit_v4_title_provider_on_connection(conn, store[3], prepared, model=prepared.request.kwargs["model"])
    with store[0].connect() as conn:
        physical = conn.execute(select(attempts).where(attempts.c.attempt_id == admission.attempt.attempt_id)).one()

    values = title_result_inputs()
    descriptor = _descriptor(prepared.input)
    wire = json.loads(descriptor.wire_json)
    root = decode_continuation_snapshot(admission.selected.payload_json)
    completion = _issued_title_completion(admission.attempt.attempt_id)
    diagnostic = diagnostic_provider_metadata(completion.provider_metadata)
    initial = values["audit_call"]
    call = replace(
        initial,
        model_requested=wire["options"]["model"],
        messages_hash=stable_hash(wire["messages"]),
        private_request=capture_provider_context(messages=wire["messages"], tools=None),
        temperature=wire["options"].get("temperature"),
        seed=wire["options"].get("seed"),
        started_at=restore_utc(physical.started_at),
        # This direct SQL fixture has no live SDK elapsed time. Its audit
        # completion must precede the database-clock response observation.
        finished_at=restore_utc(physical.started_at),
        call_id=admission.attempt.attempt_id,
        private_diagnostics=(),
        model_returned=diagnostic.model_returned,
        provider_request_id=diagnostic.provider_request_id,
        finish_reason=diagnostic.finish_reason,
        reasoning_content=diagnostic.reasoning_content,
        reasoning_details=diagnostic.reasoning_details,
    )
    captured = capture_title_provider_result(
        logical=root.logical,
        actor_user_id=prepared.actor_user_id,
        effect_key="provider:3:0",
        input_sha256=descriptor.input_sha256,
        creator_sequence=running.claim.segment_sequence,
        creator_generation=running.claim.ownership_generation,
        completion=completion,
        audit_call=call,
        context=running.session_operation_context,
        audit_drafts=provider_attempt_audit_drafts(call, running.session_operation_context),
        env_ref_names=prepared.names,
    )
    batch = build_v4_title_complete_batch(prepared=prepared, admission=admission, captured=captured, published_at=call.finished_at)
    require_v4_title_complete_batch_matches_capture(prepared=prepared, admission=admission, captured=captured, batch=batch)
    forged_received = prepare_snapshot_append(
        admission.selected,
        snapshot_json=batch.received.snapshot_json,
        events=(ContinuationEvent("checkpoint", batch.received.events[0].payload_json + " "),),
        published_at=call.finished_at,
    )
    with pytest.raises(AuditIntegrityError, match="received result or original transition"):
        require_v4_title_complete_batch_matches_capture(
            prepared=prepared, admission=admission, captured=captured, batch=replace(batch, received=forged_received)
        )
    altered_private = replace(
        captured,
        completion=replace(
            captured.completion,
            provider_metadata=replace(captured.completion.provider_metadata, reasoning_details=[{"text": "changed private detail"}]),
        ),
    )
    with pytest.raises(AuditIntegrityError, match="original issued metadata capture"):
        store[3].complete_v4_title_result_sync(
            prepared=prepared,
            admission=admission,
            captured=altered_private,
            names=CurrentTitleNames(running, admission.selected, prepared.names),
        )
    result = store[3].complete_v4_title_result_sync(
        prepared=prepared,
        admission=admission,
        captured=captured,
        names=CurrentTitleNames(running, admission.selected, prepared.names),
    )
    with store[0].connect() as conn:
        effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
        physical = conn.execute(select(attempts).where(attempts.c.attempt_id == admission.attempt.attempt_id)).one()
        ledger = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
    assert result.result == captured.chunk and result.audit_call == call
    assert result.selected.cursor == admission.selected.cursor + 2
    assert effect.disposition == "committed" and effect.provider_attempt_id == admission.attempt.attempt_id
    assert hold.state == "consumed" and hold.provider_attempt_id == admission.attempt.attempt_id
    assert physical.ledger_entry_id is not None and len(ledger) == 1
    assert ledger[0].model == completion.provider_metadata.model_returned
    with store[0].connect() as conn:
        nested = conn.begin_nested()
        proof = _verified_v4_title_stored_source_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            sequence=running.claim.segment_sequence,
            generation=running.claim.ownership_generation,
            selected=result.selected,
            names=prepared.names,
            title_reference=result.result.reference,
        )
        assert proof is not None
        with pytest.raises(AuditIntegrityError, match="verified stored V4 source"):
            decode_title_provider_result(result.result, env_ref_names=prepared.names, stored_v4_source=replace(proof))
        nested.rollback()
        with pytest.raises(AuditIntegrityError, match="verified stored V4 source"):
            decode_title_provider_result(result.result, env_ref_names=prepared.names, stored_v4_source=proof)


@pytest.mark.asyncio
async def test_v4_title_complete_result_exceeding_aggregate_cap_settles_honest_terminal(operation_store, tmp_path, monkeypatch):
    allowance = 64 * 1024
    policy = continuation_policy_v4_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=allowance))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, _composer_service, _work, prepared, _guard_requests = await _historical_prepared_title(store, policy, tmp_path, monkeypatch)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(store[0], sid) as conn:
        admission = admit_v4_title_provider_on_connection(conn, store[3], prepared, model=prepared.request.kwargs["model"])
    with store[0].connect() as conn:
        physical = conn.execute(select(attempts).where(attempts.c.attempt_id == admission.attempt.attempt_id)).one()

    values = title_result_inputs()
    descriptor = _descriptor(prepared.input)
    wire = json.loads(descriptor.wire_json)
    root = decode_continuation_snapshot(admission.selected.payload_json)
    reasoning = "r" * 100_000
    completion = _issued_title_completion(admission.attempt.attempt_id, reasoning=reasoning)
    diagnostic = diagnostic_provider_metadata(completion.provider_metadata)
    initial = values["audit_call"]
    call = replace(
        initial,
        model_requested=wire["options"]["model"],
        messages_hash=stable_hash(wire["messages"]),
        private_request=capture_provider_context(messages=wire["messages"], tools=None),
        temperature=wire["options"].get("temperature"),
        seed=wire["options"].get("seed"),
        started_at=restore_utc(physical.started_at),
        # This direct SQL fixture has no live SDK elapsed time. Its audit
        # completion must precede the database-clock response observation.
        finished_at=restore_utc(physical.started_at),
        call_id=admission.attempt.attempt_id,
        private_diagnostics=(),
        model_returned=diagnostic.model_returned,
        provider_request_id=diagnostic.provider_request_id,
        finish_reason=diagnostic.finish_reason,
        reasoning_content=diagnostic.reasoning_content,
        reasoning_details=diagnostic.reasoning_details,
    )
    captured = capture_title_provider_result(
        logical=root.logical,
        actor_user_id=prepared.actor_user_id,
        effect_key="provider:3:0",
        input_sha256=descriptor.input_sha256,
        creator_sequence=running.claim.segment_sequence,
        creator_generation=running.claim.ownership_generation,
        completion=completion,
        audit_call=call,
        context=running.session_operation_context,
        audit_drafts=provider_attempt_audit_drafts(call, running.session_operation_context),
        env_ref_names=prepared.names,
    )
    assert len(captured.chunk.canonical.encode("utf-8")) < 1_048_576
    terminal = store[3].complete_v4_title_result_sync(
        prepared=prepared,
        admission=admission,
        captured=captured,
        names=CurrentTitleNames(running, admission.selected, prepared.names),
    )
    with store[0].connect() as conn:
        cold = read_v4_title_paid_overflow_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
        )
        effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
        ledger = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
    assert cold == terminal and cold is not None
    assert (cold.receipt.reason, cold.receipt.representation, cold.receipt.measurement) == (
        "aggregate_retained_bytes",
        "retained_history",
        "at_least",
    )
    assert (cold.receipt.limit_bytes, cold.receipt.observed_bytes) == (allowance, allowance + 1)
    assert cold.receipt.evidence_status == "incomplete" and cold.receipt.retryable is False
    assert effect.disposition == "paid_overflow" and hold.state == "consumed" and len(ledger) == 1
    assert reasoning not in cold.terminal.payload_json
