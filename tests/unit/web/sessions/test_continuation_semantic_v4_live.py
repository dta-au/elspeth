"""V4 semantic Advisor admits the original physical call and closes each retained outcome."""

from __future__ import annotations

import json

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_semantic_advisor_phase import (
    SemanticAdvisorNoSdkTerminal,
    SemanticAdvisorPaidTerminal,
    run_continuation_semantic_advisor,
)
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_semantic_paid_overflow_terminal import read_v4_semantic_paid_overflow_terminal_on_connection
from elspeth.web.sessions.continuation_semantic_v4_no_sdk_terminal import read_v4_semantic_no_sdk_terminal_on_connection
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
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
        assert len(entries) == 1 and entries[0].model == sdk[0]


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
            return ModelResponse(
                model="test/model",
                choices=[
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(_answer(case["request"])),
                            "reasoning_details": [{"text": "x" * 200_000} for _ in range(7)],
                            "tool_calls": None,
                        },
                    }
                ],
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
            )

        monkeypatch.setattr(litellm, "acompletion", completion)
        phases = _phases(case)
        result = await run_continuation_semantic_advisor(phases, case["runtime"].source.selected)
        assert type(result) is SemanticAdvisorPaidTerminal and len(sdk) == 1
        running = case["running"]
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
            physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
            entries = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
            cold = read_v4_semantic_paid_overflow_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect.effect_key
            )
        assert cold == result.verified and cold.hold_state == "consumed"
        assert effect.disposition == "paid_overflow" and hold.state == "consumed"
        assert physical.settled_at is not None and physical.ledger_entry_id == entries[0].entry_id
        assert len(entries) == 1 and entries[0].model == sdk[0]
