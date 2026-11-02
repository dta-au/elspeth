"""Actual V4 TITLE provider success joins paid custody to one SQL settlement."""

from __future__ import annotations

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.web.composer import continuation_title_observation as title_observation
from elspeth.web.composer.continuation_codec import measure_continuation_response_size
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_provider_outcomes import measure_physical_audit_call_size
from elspeth.web.composer.continuation_title_provider import run_prepared_title_provider
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_title_paid_overflow_terminal import (
    VerifiedTitlePaidOverflowTerminal,
    read_v4_title_paid_overflow_terminal_on_connection,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_title_provider import raw_reply
from tests.unit.web.sessions.test_continuation_v3_title_paid_tail_preflight import _historical_prepared_title

operation_store = operations.operation_store


@pytest.mark.asyncio
async def test_actual_v4_title_provider_keeps_complete_response_and_one_credited_ledger(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, _composer_service, work, prepared, _guard_requests = await _historical_prepared_title(store, policy, tmp_path, monkeypatch)
    sdk_calls: list[str] = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs["model"])
        return raw_reply(kwargs["model"])

    monkeypatch.setattr(litellm, "acompletion", completion)
    result = await run_prepared_title_provider(
        prepared=prepared, invocation_owner=ProviderInvocationOwner(service=store[3], required_work=work)
    )
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with store[0].connect() as conn:
        effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
        physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
        ledger = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
    assert result is not None and result.selected.cursor == prepared.selected.cursor + 3
    assert len(sdk_calls) == 1 and effect.disposition == "committed"
    assert hold.state == "consumed" and hold.provider_attempt_id == effect.provider_attempt_id
    assert physical.ledger_entry_id is not None and len(ledger) == 1
    assert ledger[0].source == "auto_title" and ledger[0].model == sdk_calls[0]


@pytest.mark.asyncio
async def test_actual_v4_title_oversized_paid_response_returns_cold_terminal_without_second_sdk(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, _composer_service, work, prepared, _guard_requests = await _historical_prepared_title(store, policy, tmp_path, monkeypatch)
    sdk_calls: list[str] = []
    observed_sizes: list[tuple[int | None, bool]] = []
    original_observer = title_observation.observe_title_provider_result

    def observe_actual(**kwargs):
        physical_size = measure_physical_audit_call_size(kwargs["audit_call"])
        response_size = measure_continuation_response_size(kwargs["completion"], audit_call=kwargs["audit_call"])
        observed_sizes.append((physical_size.admitted_bytes, response_size.exceeded))
        return original_observer(**kwargs)

    monkeypatch.setattr(title_observation, "observe_title_provider_result", observe_actual)

    async def completion(**kwargs):
        sdk_calls.append(kwargs["model"])
        return ModelResponse(
            id="large-title-paid-response",
            model=kwargs["model"],
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "z" * 230_000,
                        "reasoning_content": "x" * 60_000,
                        "reasoning_details": [{"text": "y" * 60_000} for _ in range(8)],
                    },
                }
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    result = await run_prepared_title_provider(
        prepared=prepared, invocation_owner=ProviderInvocationOwner(service=store[3], required_work=work)
    )
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with store[0].connect() as conn:
        cold = read_v4_title_paid_overflow_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
        )
        effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
        ledger = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
    assert type(result) is VerifiedTitlePaidOverflowTerminal and result == cold
    assert len(observed_sizes) == 1 and observed_sizes[0][0] is not None and observed_sizes[0][1]
    assert (result.receipt.reason, result.receipt.representation, result.receipt.evidence_status) == (
        "private_document_bytes",
        "response",
        "incomplete",
    )
    assert result.receipt.retryable is False and result.receipt.observed_bytes == 1_048_577
    assert len(sdk_calls) == 1 and effect.disposition == "paid_overflow"
    assert hold.state == "consumed" and len(ledger) == 1 and ledger[0].model == sdk_calls[0]
