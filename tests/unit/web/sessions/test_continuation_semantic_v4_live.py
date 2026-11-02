"""V4 semantic Advisor admits the original physical call and closes each retained outcome."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_semantic_advisor as semantic_physical
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.bounded_json import PROVIDER_ARTIFACT_UNAVAILABLE
from elspeth.web.composer.continuation_codec import decode_continuation_response
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_semantic_advisor_phase import (
    SemanticAdvisorNoSdkTerminal,
    SemanticAdvisorPaidIncompleteTerminal,
    run_continuation_semantic_advisor,
)
from elspeth.web.composer.provider_audit_source import ProviderAuditSource
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete
from elspeth.web.composer.visible_tool_event import visible_response_gap_for_source
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_cost import _closed_call
from elspeth.web.sessions.continuation_semantic_advisor import read_semantic_advisor_outcome_on_connection
from elspeth.web.sessions.continuation_semantic_paid_incomplete_terminal import read_v4_semantic_paid_incomplete_terminal_on_connection
from elspeth.web.sessions.continuation_semantic_v4_no_sdk_terminal import read_v4_semantic_no_sdk_terminal_on_connection
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_continuation_semantic_advisor import _answer, _reply
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_semantic_advisor import _semantic_case
from tests.unit.web.sessions.test_continuation_semantic_advisor import operation_store as _operation_store

operation_store = _operation_store


def _v4_store(store, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    return store[0], store[1], authority, store[3], store[4]


def _phases(case):
    runtime = case["runtime"]
    phases = ContinuationLivePhases(
        service=runtime.materialization.service,
        authority=runtime.materialization.authority,
        work=case["work"],
        provider_owner=runtime.bindings.provider_owner,
        lease=case["lease"],
        progress=runtime.bindings.progress,
    )
    phases.bind_initial(runtime)
    return phases


def _unretainable_reply(request):
    return ModelResponse(
        model="test/model",
        choices=[
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(_answer(request)),
                    "reasoning_details": [{"text": "x" * 200_000} for _ in range(7)],
                    "tool_calls": None,
                },
            }
        ],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
    )


def _large_complete_reply(request):
    response = _reply(json.dumps(_answer(request)))
    response.choices[0].message.reasoning_content = "r" * 100_000
    return response


@pytest.mark.asyncio
async def test_v4_semantic_success_is_one_original_complete_result_and_consumed_hold(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        assert type(case["runtime"].source.policy) is ContinuationAdmissionPolicyV4
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _reply(json.dumps(_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        phases = _phases(case)
        result = await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert result is None and len(sdk) == 1
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
        assert effect.disposition == "committed" and hold.state == "consumed"
        assert physical.settled_at is not None and physical.ledger_entry_id == entries[0].entry_id
        assert effect.requested_model == sdk[0]
        assert len(entries) == 1 and entries[0].model == "test/model"


@pytest.mark.asyncio
async def test_v4_semantic_large_original_is_retained_with_bounded_audit_and_cold_proof(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _large_complete_reply(case["request"])

        monkeypatch.setattr(litellm, "acompletion", completion)
        phases = _phases(case)
        result = await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert result is None and len(sdk) == 1 and phases.latest_snapshot is not None
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            cold = read_semantic_advisor_outcome_on_connection(conn, running, selected=phases.latest_snapshot, effect_key=effect.effect_key)
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
        assert effect.disposition == "committed" and hold.state == "consumed"
        assert cold.response is not None
        assert decode_continuation_response(cold.response).provider_metadata.reasoning_content == "r" * 100_000
        assert cold.audit_call.reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE


@pytest.mark.asyncio
async def test_v4_semantic_changed_original_metadata_is_rejected_before_paid_settlement(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _large_complete_reply(case["request"])

        original_capture = semantic_physical._capture_composer_llm_completion_fields

        def changed_original(*args, **kwargs):
            message, tool_calls, metadata = original_capture(*args, **kwargs)
            return message, tool_calls, replace(metadata, reasoning_content="changed after SDK return")

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(semantic_physical, "_capture_composer_llm_completion_fields", changed_original)
        phases = _phases(case)
        with pytest.raises(BaseExceptionGroup) as rejected:
            await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert len(rejected.value.exceptions) == 2
        assert all(
            type(error) is AuditIntegrityError and str(error) == "V4 result lost its original issued metadata capture"
            for error in rejected.value.exceptions
        )
        assert len(sdk) == 1
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
        assert effect.disposition == "dispatched" and hold.state == "active" and entries == []


@pytest.mark.asyncio
async def test_v4_semantic_changed_bounded_audit_is_rejected_before_paid_settlement(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _large_complete_reply(case["request"])

        original_record = semantic_physical.build_llm_call_record

        def changed_audit(*args, **kwargs):
            call = original_record(*args, **kwargs)
            return replace(call, reasoning_content="changed bounded audit") if call.status.value == "success" else call

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(semantic_physical, "build_llm_call_record", changed_audit)
        phases = _phases(case)
        with pytest.raises(AuditIntegrityError):
            await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert len(sdk) == 1
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
        assert effect.disposition == "dispatched" and hold.state == "active" and entries == []


@pytest.mark.asyncio
async def test_v4_semantic_pre_sdk_failure_closes_original_zero_use_and_cold_terminal(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        assert type(case["runtime"].source.policy) is ContinuationAdmissionPolicyV4
        sdk = []

        async def completion(**_kwargs):
            sdk.append(1)
            raise AssertionError("SDK must remain unentered")

        original = ProviderCallCustody.require_cost_dispatch

        def refuse_after_admission(self):
            if self.continuation_semantic_v4_admission is not None:
                raise TimeoutError("injected semantic pre-SDK after original H")
            return original(self)

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", refuse_after_admission)
        phases = _phases(case)
        result = await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert type(result) is SemanticAdvisorNoSdkTerminal and not sdk
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
            cold = read_v4_semantic_no_sdk_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
            )
        assert cold is not None and cold.hold_state == "consumed"
        assert effect.disposition == "refused" and hold.state == "consumed"
        assert physical.settled_at is not None and physical.ledger_entry_id == entries[0].entry_id
        assert len(entries) == 1 and entries[0].prompt_tokens == entries[0].completion_tokens == 0


@pytest.mark.asyncio
async def test_v4_semantic_unretainable_returned_audit_is_one_paid_terminal_without_retry(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        assert type(case["runtime"].source.policy) is ContinuationAdmissionPolicyV4
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _unretainable_reply(case["request"])

        monkeypatch.setattr(litellm, "acompletion", completion)
        phases = _phases(case)
        result = await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert type(result) is SemanticAdvisorPaidIncompleteTerminal and len(sdk) == 1
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
            cold = read_v4_semantic_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
            )
        assert cold == result.verified and cold.hold_state == "consumed"
        assert cold.receipt.reason == "capture_budget_exceeded" and cold.receipt.capture_surface == "provider_metadata"
        assert effect.disposition == "paid_incomplete" and hold.state == "consumed"
        assert physical.settled_at is not None and physical.ledger_entry_id == entries[0].entry_id
        assert len(entries) == 1 and entries[0].model == sdk[0]
    assert type(effect.provider_attempt_id) is str
    with store[0].connect() as conn:
        original_rows = conn.execute(
            select(messages).where(
                messages.c.session_id == sid, messages.c.role == "audit", messages.c.content == "Provider call result recorded."
            )
        ).all()
        original_audits = [row for row in original_rows if effect.provider_attempt_id in json.dumps(row.tool_calls)]
        assert len(original_audits) == 1
        original_row = original_audits[0]
        original_call = _closed_call(
            original_row,
            ProviderAuditSource(cold.source.session_id, cold.source.session_operation_id, cold.source.session_operation_epoch),
            effect.provider_attempt_id,
        )
    assert original_call.messages_hash != "0" * 64
    audit_source = ProviderAuditSource(cold.source.session_id, cold.source.session_operation_id, cold.source.session_operation_epoch)

    def write_public_call(call):
        envelope = llm_call_audit_envelope(call)
        gap = visible_response_gap_for_source(call, audit_source, envelope)
        with store[0].begin() as conn:
            conn.execute(update(messages).where(messages.c.id == original_row.id).values(tool_calls=[envelope, gap]))

    write_public_call(replace(original_call, messages_hash="0" * 64))
    with store[0].connect() as conn, pytest.raises(AuditIntegrityError, match="original physical request"):
        read_v4_semantic_paid_incomplete_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
        )
    assert effect.response_finished_at is not None
    forged_finish = restore_utc(effect.response_finished_at) + timedelta(seconds=1)
    write_public_call(replace(original_call, finished_at=forged_finish))
    with store[0].connect() as conn, pytest.raises(AuditIntegrityError, match="recorded physical time bucket"):
        read_v4_semantic_paid_incomplete_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
        )
    write_public_call(original_call)
    with store[0].begin() as conn:
        assert (
            read_v4_semantic_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
            )
            == cold
        )
        assert conn.dialect.name == "sqlite"
        original_trigger = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_chat_messages_no_delete'"
        ).scalar_one()
        savepoint = conn.begin_nested()
        try:
            conn.exec_driver_sql("DROP TRIGGER trg_chat_messages_no_delete")
            conn.execute(delete(messages).where(messages.c.id == original_row.id))
            with pytest.raises(AuditIntegrityError, match="public row"):
                read_v4_semantic_paid_incomplete_terminal_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
                )
        finally:
            savepoint.rollback()
        assert (
            conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_chat_messages_no_delete'").scalar_one()
            == original_trigger
        )
        assert (
            read_v4_semantic_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
            )
            == cold
        )
        with pytest.raises(IntegrityError, match="chat_messages rows are append-only"):
            conn.execute(delete(messages).where(messages.c.id == original_row.id))


@pytest.mark.asyncio
async def test_v4_semantic_direct_paid_incomplete_sql_rejects_forged_parser_exception(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        assert type(case["runtime"].source.policy) is ContinuationAdmissionPolicyV4
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs["model"])
            return _unretainable_reply(case["request"])

        monkeypatch.setattr(litellm, "acompletion", completion)
        original_sql = SessionServiceImpl.complete_v4_semantic_paid_incomplete_sync

        def forged_sql(self, *, prepared, admission, incomplete, no_sdk_issuer):
            copied = V4ProviderResultIncomplete(sdk_return=incomplete.sdk_return, metadata=incomplete.metadata, losses=incomplete.losses)
            return original_sql(self, prepared=prepared, admission=admission, incomplete=copied, no_sdk_issuer=no_sdk_issuer)

        monkeypatch.setattr(SessionServiceImpl, "complete_v4_semantic_paid_incomplete_sync", forged_sql)
        phases = _phases(case)
        with pytest.raises(AuditIntegrityError):
            await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert len(sdk) == 1
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
        assert effect.disposition == "dispatched" and hold.state == "active"
        assert physical.settled_at is None and physical.ledger_entry_id is None and entries == []
