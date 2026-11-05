"""Genuine EARLY complete returns retain H and atomically settle C/K or C/R."""

from __future__ import annotations

import json
from dataclasses import replace

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
from elspeth.web.composer.continuation_checkpoint_early_observation import observe_v4_early_checkpoint_returned_response
from elspeth.web.composer.continuation_checkpoint_early_physical import (
    checkpoint_early_detached_runtime,
    checkpoint_early_format_retry,
    checkpoint_early_physical_custody,
)
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.continuation_checkpoint_projection import (
    CheckpointPassAccounting,
    ProjectedEarlyCheckpoint,
    project_early_checkpoint_result,
)
from elspeth.web.composer.continuation_clock import ContinuationActionClock, SegmentFit, admit_action
from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_next_action import iteration_stage_action_policy
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.sessions import continuation_iteration_return as stages
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_checkpoint_early_sql import complete_early_checkpoint_sync, retain_early_complete_return_sync
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import _early_attempt, _early_case, _invoke_early
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

_CLEAN = json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The approach is coherent.", "note": None})
operation_store = _operation_store


def _pass_rows(conn, session_id):
    return tuple(
        row
        for row in conn.execute(select(messages).where(messages.c.session_id == session_id))
        if row.tool_calls and any(envelope.get("_kind") == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND for envelope in row.tool_calls)
    )


async def _complete_original(source, *, reject_copied_return=False):
    """Use only the original audited SDK result, issued projection and SQL owners."""
    owner = source.phase_owner
    runtime, phases = owner.runtime, owner.phases
    custody = checkpoint_early_physical_custody(source)
    prepared, admission = custody.continuation_checkpoint_v4, custody.continuation_checkpoint_v4_admission
    assert prepared is not None and admission is not None and custody.call is not None
    returned = observe_v4_early_checkpoint_returned_response(
        source.capture.snapshot(),
        custody=custody,
        policy=runtime.source.policy,
        attempt=admission.attempt,
        context=runtime.bindings.session_operation_context,
        env_ref_names=runtime.env_ref_names,
    )
    assert type(returned) is CheckpointReturnedResponseObservation
    selected = await load_selected_continuation(
        authority=phases.authority, running=prepared.running, selected=admission.selected, required_work=phases.work
    )
    inventory = await materialize_continuation_env_inventory(
        advisor=phases.service._advisor_checkpoint, authority=phases.authority, selected=selected, required_work=phases.work
    )
    retry = checkpoint_early_format_retry(source)
    accounting = (
        CheckpointPassAccounting(0, 1, None, None, False)
        if retry is None
        else CheckpointPassAccounting(
            0,
            2,
            retry.marker.first_attempt_schema_valid,
            retry.child.accounting.first_attempt_accepted,
            retry.marker.format_reprompt_required,
        )
    )
    projected = await project_early_checkpoint_result(
        prepared=prepared,
        admission=admission,
        detached=checkpoint_early_detached_runtime(source),
        original=runtime,
        selected=selected,
        inventory=inventory,
        custody=custody,
        returned=returned,
        accounting=accounting,
        physical_source=source,
    )
    assert type(projected) is ProjectedEarlyCheckpoint
    service = prepared.composer._require_sessions_service()
    if reject_copied_return:
        before = stored_rows(service._engine)
        with pytest.raises(AuditIntegrityError, match="original"):
            retain_early_complete_return_sync(service=service, prepared=prepared, admission=admission, returned=replace(returned))
        assert stored_rows(service._engine) == before
    owed = retain_early_complete_return_sync(service=service, prepared=prepared, admission=admission, returned=returned)
    sid, operation = str(prepared.running.claim.session_id), prepared.running.claim.operation_id
    with locked_session_transaction(service._engine, sid) as conn:
        assert load_snapshot(conn, session_id=sid, operation_id=operation) == owed.owed_selected
        assert decode_continuation_snapshot(owed.owed_selected.payload_json).pending_settlement_ids == (admission.attempt.attempt_id,)
        assert conn.execute(select(holds.c.state).where(holds.c.effect_key == admission.effect_key)).scalar_one() == "active"
        assert conn.execute(select(physical.c.settled_at).where(physical.c.attempt_id == admission.attempt.attempt_id)).scalar_one() is None
        assert not _pass_rows(conn, sid)
    advanced = complete_early_checkpoint_sync(service=service, prepared=prepared, owed=owed, returned=returned, projected=projected)
    with locked_session_transaction(service._engine, sid) as conn:
        assert load_snapshot(conn, session_id=sid, operation_id=operation) == advanced.completed
        assert advanced.completed.cursor == owed.owed_selected.cursor + 2
        assert not decode_continuation_snapshot(advanced.completed.payload_json).pending_settlement_ids
        assert conn.execute(select(holds.c.state).where(holds.c.effect_key == admission.effect_key)).scalar_one() == "consumed"
        child = conn.execute(select(effects).where(effects.c.effect_key == admission.effect_key)).one()
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
        usage = conn.execute(select(ledger).where(ledger.c.entry_id == attempt.ledger_entry_id)).one()
        assert child.disposition == "committed" and child.provider_attempt_id == admission.attempt.attempt_id
        assert attempt.settled_at is not None
        assert usage.prompt_tokens == returned.audit_call.prompt_tokens == 7
        assert usage.completion_tokens == returned.audit_call.completion_tokens == 3
        assert returned.audit_call.provider_cost == 0.01
        stage = stages.read_iteration_stage_data_on_connection(
            conn, session_id=prepared.running.claim.session_id, operation_id=operation, selected=advanced.completed
        )
        assert stage is not None and stage.stage == ("early_retry_ready" if projected.retry_pending else "classify_ready")
        passes = _pass_rows(conn, sid)
        assert len(passes) == (0 if projected.retry_pending else 1)
        if passes:
            assert projected.pass_record is not None
            assert passes[0].tool_calls == [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": projected.pass_record.to_dict()}]
    return advanced, returned, projected


async def _admit_original_retry(owner, running, completed):
    """Run the driver's policy/fit/clock sequence against the actual R head."""
    selected = await load_selected_continuation(authority=owner.authority, running=running, selected=completed, required_work=owner.work)
    runtime, _inventory = await owner.current(selected)
    with locked_session_transaction(owner.authority._engine, str(running.claim.session_id)) as conn:
        stage = stages.read_iteration_stage_data_on_connection(
            conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, selected=completed
        )
    assert stage is not None and stage.stage == "early_retry_ready"
    policy = runtime.source.policy
    action = iteration_stage_action_policy(selected=completed, stage=stage, policy=policy)
    timing = owner.authority.read_continuation_timing(running)
    assert (
        admit_action(
            action,
            policy.segment,
            hard_remaining=timing.hard_remaining_seconds,
            cooperative_remaining=timing.cooperative_remaining_seconds,
            fresh=False,
        )
        is SegmentFit.ADMIT
    )
    original_clock = owner.action_clock
    clock = ContinuationActionClock(action)
    deadline = owner.work.coordinator.sql_deadline_owner
    assert deadline is not None
    deadline.begin_action(action, started_at=clock.started_monotonic)
    owner.bind_action_clock(clock)
    assert clock is not original_clock
    return selected


@pytest.mark.asyncio
async def test_actual_early_clean_return_retains_h_then_atomically_commits_child_and_classify_ready(operation_store, tmp_path, monkeypatch):
    async with _early_case(operation_store, tmp_path, monkeypatch) as (_store, owner, _running, selected, primary_sdk):
        requests = []

        async def complete(**request):
            requests.append(request)
            return _reply(_CLEAN)

        monkeypatch.setattr(litellm, "acompletion", complete)
        async with _early_attempt(owner, selected) as source:
            text, _metadata = await _invoke_early(source)
            assert text == _CLEAN
            advanced, returned, projected = await _complete_original(source, reject_copied_return=True)
            assert advanced.retry is None and not projected.retry_pending
            assert returned.audit_call.status is ComposerLLMCallStatus.SUCCESS
            assert projected.accounting == CheckpointPassAccounting(0, 1, True, True, False)
            assert projected.injection is None and len(requests) == len(primary_sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "tool_calls", "schema_valid", "reprompt", "malformed_audit"),
    [
        pytest.param("not JSON", False, False, True, False, id="invalid-json"),
        pytest.param("", False, False, True, True, id="empty"),
        pytest.param("   ", False, False, True, True, id="whitespace"),
        pytest.param(None, False, None, False, True, id="null"),
        pytest.param(None, True, None, False, True, id="tool-null"),
        pytest.param(_CLEAN, True, False, True, True, id="tool-clean-text"),
    ],
)
async def test_actual_early_complete_malformed_first_result_consumes_h_before_retry_ready(
    operation_store, tmp_path, monkeypatch, content, tool_calls, schema_valid, reprompt, malformed_audit
):
    async with _early_case(operation_store, tmp_path, monkeypatch) as (_store, owner, running, selected, primary_sdk):
        requests = []

        async def complete(**request):
            requests.append(request)
            if not tool_calls:
                return _reply(content)
            return ModelResponse(
                model="test/model",
                choices=[
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": content,
                            "tool_calls": [
                                {
                                    "id": "unexpected-advisor-call",
                                    "type": "function",
                                    "function": {"name": "set_pipeline", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
            )

        monkeypatch.setattr(litellm, "acompletion", complete)
        async with _early_attempt(owner, selected) as source:
            if malformed_audit:
                with pytest.raises(_MalformedLLMResponseError):
                    await _invoke_early(source)
            else:
                text, _metadata = await _invoke_early(source)
                assert text == content
            advanced, returned, projected = await _complete_original(source)
            assert advanced.retry is not None and projected.retry_pending
            marker, child = advanced.retry.marker, advanced.retry.child
            assert marker.format_reprompt_required is reprompt
            assert marker.first_attempt_schema_valid is schema_valid
            assert child.accounting.first_attempt_accepted is (False if content is not None else None)
            assert projected.pass_record is None and projected.injection is None
            assert returned.audit_call.status is (
                ComposerLLMCallStatus.MALFORMED_RESPONSE if malformed_audit else ComposerLLMCallStatus.SUCCESS
            )
            retained = decode_continuation_response(child.response)
            assert retained.message.content == content
            assert retained.tool_batch == returned.tool_batch
            assert bool(retained.tool_batch.calls) is tool_calls
            assert len(requests) == len(primary_sdk) == 1

        # R is DATA until this distinct action passes the driver's actual
        # retained policy and segment-fit check and receives its own clock.
        retry_selected = await _admit_original_retry(owner, running, advanced.completed)

        async def second_complete(**request):
            requests.append(request)
            return _reply(_CLEAN)

        monkeypatch.setattr(litellm, "acompletion", second_complete)
        async with _early_attempt(owner, retry_selected) as second_source:
            assert second_source.clock is owner.action_clock and checkpoint_early_format_retry(second_source) is not None
            text, _metadata = await _invoke_early(second_source)
            assert text == _CLEAN
            final, second_returned, second_projected = await _complete_original(second_source)
            assert final.retry is None and not second_projected.retry_pending
            assert second_projected.accounting == CheckpointPassAccounting(
                0, 2, schema_valid, False if content is not None else None, reprompt
            )
            second_custody = checkpoint_early_physical_custody(second_source)
            assert second_custody.continuation_checkpoint_v4.retry_ordinal == 1
            assert second_returned.audit_call.call_id != returned.audit_call.call_id
            assert second_returned.audit_call.status is ComposerLLMCallStatus.SUCCESS
            assert len(requests) == 2 and len(primary_sdk) == 1
