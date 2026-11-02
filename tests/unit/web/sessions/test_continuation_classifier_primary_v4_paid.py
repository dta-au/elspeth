"""Real V4 classifier PRIMARY complete and paid-overflow custody."""

from __future__ import annotations

from contextlib import asynccontextmanager

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_classifier_primary import ClassifierPrimaryResult, VerifiedClassifierPaidOverflowTerminal
from elspeth.web.sessions.continuation_classifier_primary_terminal import read_v4_classifier_paid_overflow_terminal_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_cold_restore as cold_fixture
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_classifier_primary import _classifier_case, _invoke

operation_store = operations.operation_store


@asynccontextmanager
async def _v4_classifier_case(store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    monkeypatch.setattr(cold_fixture, "minimal_continuation_policy", lambda: policy)
    v4_store = (store[0], store[1], authority, store[3], store[4])
    async with _classifier_case(v4_store, tmp_path, monkeypatch) as case:
        assert type(case[3].runtime.source.policy) is type(policy)
        yield v4_store, case


def _reply(content: str) -> ModelResponse:
    return ModelResponse(
        model="test/model",
        choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": content, "tool_calls": None}}],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ("staged_review", "composition_last_chance"))
async def test_v4_classifier_complete_retains_actual_paid_result_and_consumes_hold(operation_store, tmp_path, monkeypatch, branch):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, prior_sdk = case
        physical = []

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply("Original V4 classifier reply")

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _invoke(issuer, runtime, prepared, branch=branch)
        assert type(result) is ClassifierPrimaryResult
        assert result is issuer.result and issuer.terminal is None
        assert result.completion.message.content == "Original V4 classifier reply"
        assert issuer.sdk_entered and len(physical) == 1 and len(prior_sdk) == 1
        sid = str(running.claim.session_id)
        with store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.effect_key == result.effect_key)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.effect_key == result.effect_key)).one()
            attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
            ledgers = conn.execute(select(usage).where(usage.c.session_id == sid, usage.c.entry_id == attempt.ledger_entry_id)).all()
        assert effect.disposition == "committed" and effect.receipt_key is not None
        assert hold.state == "consumed" and hold.provider_attempt_id == effect.provider_attempt_id
        assert attempt.settled_at is not None and attempt.ledger_entry_id is not None and len(ledgers) == 1


@pytest.mark.asyncio
async def test_v4_classifier_paid_overflow_has_one_cold_terminal_and_detects_tamper(operation_store, tmp_path, monkeypatch):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, prior_sdk = case
        physical = []

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply("X" * (1_048_576 + 1))

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await _invoke(issuer, runtime, prepared)
        assert type(result) is VerifiedClassifierPaidOverflowTerminal
        assert result is issuer.terminal and issuer.result is None
        assert issuer.sdk_entered and len(physical) == 1 and len(prior_sdk) == 1
        sid, operation, effect_key = str(running.claim.session_id), running.claim.operation_id, result.receipt.identity.effect_key
        with locked_session_transaction(store[0], sid) as conn:
            cold = read_v4_classifier_paid_overflow_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect_key
            )
            assert cold is not None and cold.receipt == result.receipt and cold.hold_state == "consumed"
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.effect_key == effect_key)).one()
            hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.effect_key == effect_key)).one()
            attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == result.receipt.identity.attempt_id)).one()
            assert effect.disposition == "paid_overflow" and hold.state == "consumed"
            assert attempt.settled_at is not None and attempt.ledger_entry_id is not None
            savepoint = conn.begin_nested()
            try:
                assert (
                    conn.execute(
                        update(effects)
                        .where(effects.c.session_id == sid, effects.c.effect_key == effect_key)
                        .values(requested_model="forged/model")
                    ).rowcount
                    == 1
                )
                with pytest.raises(AuditIntegrityError):
                    read_v4_classifier_paid_overflow_terminal_on_connection(
                        conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect_key
                    )
            finally:
                savepoint.rollback()
            assert (
                read_v4_classifier_paid_overflow_terminal_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect_key
                )
                == cold
            )
