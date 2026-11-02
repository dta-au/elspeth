"""Real V4 PRIMARY gateway, physical attempt and bounded terminal controls."""

from __future__ import annotations

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.web.composer import continuation_primary as primary_module
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_codec import measure_continuation_response_size
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_provider_outcomes import measure_physical_audit_call_size
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationOwner
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_primary import PrimaryNoSdkRefusalReceipt, PrimaryPaidOverflowTerminal, PrimaryProviderResult
from elspeth.web.sessions.continuation_primary_paid_overflow_terminal import read_v4_primary_paid_overflow_terminal_on_connection
from elspeth.web.sessions.continuation_retained_capacity import _live_provider_hold_bytes, load_continuation_read_contract
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_cold_restore as cold_restore
from tests.unit.web.sessions.test_continuation_primary_result import primary_input
from tests.unit.web.sessions.test_continuation_prompt_producers import REQUEST

operation_store = test_composer_operation_authority.operation_store


async def _v4_primary_input(store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(cold_restore, "minimal_continuation_policy", lambda: policy)
    v4_store = (store[0], store[1], authority, store[3], store[4])
    kwargs, produced, recorder, _requests = await primary_input(v4_store, tmp_path, monkeypatch)
    return kwargs, produced, recorder, v4_store


@pytest.mark.asyncio
async def test_v4_primary_complete_is_one_admitted_physical_audit_ledger_and_consumed_hold(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, store = await _v4_primary_input(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    sdk = []

    async def completion(**_sdk_kwargs):
        sdk.append(1)
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
            contract = load_continuation_read_contract(conn, session_id=str(store[4]), operation_id=running.claim.operation_id)
            assert _live_provider_hold_bytes(conn, session_id=str(store[4]), operation_id=running.claim.operation_id, contract=contract)
        assert effect.disposition == "dispatched"
        assert hold.state == "active" and attempt.settled_at is None
        return ModelResponse(
            model="test/model",
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": "Actual V4 reply", "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    work = kwargs["required_work"]
    with prompt_guard_user_scope(REQUEST):
        result = await run_continuation_primary(
            service=kwargs["service"],
            authority=kwargs["authority"],
            selected=kwargs["selected"],
            producers=produced.producers,
            messages=list(produced.messages),
            tools=[],
            recorder=recorder,
            drained=RecorderDrainCursors(),
            required_work=work,
            provider_owner=ProviderInvocationOwner(service=store[3], required_work=work),
            timeout=5,
        )
    assert type(result) is PrimaryProviderResult and len(sdk) == 1
    with store[0].connect() as conn:
        effect = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
        ledgers = conn.execute(select(usage).where(usage.c.session_id == str(store[4]))).all()
        audits = conn.execute(select(messages).where(messages.c.session_id == str(store[4]), messages.c.role == "audit")).all()
    assert effect.disposition == "committed" and effect.provider_attempt_id == attempt.attempt_id
    assert hold.state == "consumed" and attempt.ledger_entry_id is not None and attempt.settled_at is not None
    assert len(ledgers) == 1 and ledgers[0].entry_id == attempt.ledger_entry_id
    assert sum(any(call.get("_kind") == "llm_call_audit" for call in row.tool_calls or ()) for row in audits) == 1


@pytest.mark.asyncio
async def test_v4_primary_unentered_attempt_cancels_zero_use_and_closes_h_without_sdk(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, store = await _v4_primary_input(operation_store, tmp_path, monkeypatch)
    sdk = []

    async def completion(**_sdk_kwargs):
        sdk.append(1)
        raise AssertionError("SDK must remain unentered")

    original = ProviderCallCustody.require_cost_dispatch

    def refuse_after_admission(self):
        if self.continuation_primary_v4_admission is not None:
            raise TimeoutError("injected pre-SDK after original H")
        return original(self)

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", refuse_after_admission)
    work = kwargs["required_work"]
    with prompt_guard_user_scope(REQUEST):
        result = await run_continuation_primary(
            service=kwargs["service"],
            authority=kwargs["authority"],
            selected=kwargs["selected"],
            producers=produced.producers,
            messages=list(produced.messages),
            tools=[],
            recorder=recorder,
            drained=RecorderDrainCursors(),
            required_work=work,
            provider_owner=ProviderInvocationOwner(service=store[3], required_work=work),
            timeout=5,
        )
    assert type(result) is PrimaryNoSdkRefusalReceipt and not sdk
    with store[0].connect() as conn:
        effect = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
        ledgers = conn.execute(select(usage).where(usage.c.session_id == str(store[4]))).all()
    assert effect.disposition == "refused" and effect.provider_attempt_id == attempt.attempt_id
    assert hold.state == "consumed" and attempt.settled_at is not None
    assert len(ledgers) == 1 and ledgers[0].entry_id == attempt.ledger_entry_id
    assert ledgers[0].prompt_tokens == ledgers[0].completion_tokens == 0


@pytest.mark.asyncio
async def test_v4_primary_oversized_actual_response_is_one_cold_paid_terminal_without_retry(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, store = await _v4_primary_input(operation_store, tmp_path, monkeypatch)
    running = kwargs["inventory"].running
    sdk = []
    measured = []

    original_observe = primary_module.observe_v4_primary_provider_result

    def observe_with_original_boundary(**values):
        outcome = values["outcome"]
        assert outcome.audit_call is not None
        measured.append(
            (
                measure_physical_audit_call_size(outcome.audit_call),
                measure_continuation_response_size(outcome.completion, audit_call=outcome.audit_call),
            )
        )
        return original_observe(**values)

    async def completion(**_sdk_kwargs):
        sdk.append(1)
        return ModelResponse(
            model="test/model",
            choices=[
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "z" * 230_000,
                        "reasoning_content": "x" * 60_000,
                        "reasoning_details": [{"text": "y" * 60_000} for _ in range(8)],
                        "tool_calls": None,
                    },
                }
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(primary_module, "observe_v4_primary_provider_result", observe_with_original_boundary)
    work = kwargs["required_work"]
    with prompt_guard_user_scope(REQUEST):
        result = await run_continuation_primary(
            service=kwargs["service"],
            authority=kwargs["authority"],
            selected=kwargs["selected"],
            producers=produced.producers,
            messages=list(produced.messages),
            tools=[],
            recorder=recorder,
            drained=RecorderDrainCursors(),
            required_work=work,
            provider_owner=ProviderInvocationOwner(service=store[3], required_work=work),
            timeout=5,
        )
    assert type(result) is PrimaryPaidOverflowTerminal and len(sdk) == 1
    assert len(measured) == 1 and measured[0][0].admitted_bytes is not None and measured[0][1].exceeded
    assert result.receipt.producer == "primary" and result.receipt.retryable is False
    assert result.receipt.representation == "response"
    assert result.receipt.measurement == "at_least" and result.receipt.observed_bytes == result.receipt.limit_bytes + 1
    with store[0].connect() as conn:
        cold = read_v4_primary_paid_overflow_terminal_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=running.claim.operation_id,
            effect_key=result.receipt.identity.effect_key,
        )
        attempts = conn.execute(select(physical).where(physical.c.session_id == str(store[4]))).all()
        retained = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
        provider_audits = conn.execute(
            select(messages).where(messages.c.session_id == str(store[4]), messages.c.role == "audit", messages.c.tool_calls.is_not(None))
        ).all()
    assert cold is not None and cold.receipt == result.receipt and cold.hold_state == "consumed"
    assert len(attempts) == 1 and attempts[0].settled_at is not None and retained.state == "consumed"
    # Guard admission is durably audited before SDK entry even when the paid
    # result later overflows. Inspect every retained envelope, so any full
    # provider audit (of any status) still fails this assertion.
    assert len(provider_audits) == 1
    guard_envelopes = provider_audits[0].tool_calls
    assert type(guard_envelopes) is list and len(guard_envelopes) == 1
    guard = guard_envelopes[0]
    assert guard["_kind"] == "prompt_guard_audit"
    assert guard["check"]["operation_id"] == running.session_operation_context.fence.operation_id
    assert guard["check"]["operation_epoch"] == running.session_operation_context.fence.operation_epoch
