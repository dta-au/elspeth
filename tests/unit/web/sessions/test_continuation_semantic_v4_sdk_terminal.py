"""Actual semantic Advisor SDK failures use the shared original-H terminal."""

import asyncio
from dataclasses import replace

import litellm
import pytest
from openai import OpenAIError
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_semantic_advisor as semantic_physical
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_semantic_advisor_phase import run_continuation_semantic_advisor
from elspeth.web.composer.provider_sdk_terminal import require_sdk_terminal_failure
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
from elspeth.web.sessions.continuation_sdk_terminal_cold import read_v4_sdk_terminal_verified_on_connection
from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, SdkTerminalResult
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from tests.unit.web.sessions.test_continuation_semantic_advisor import _semantic_case
from tests.unit.web.sessions.test_continuation_semantic_advisor import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_semantic_v4_live import _phases, _v4_store

operation_store = _operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("provider_failure", "provider_timeout", "no_observed_result"))
async def test_actual_semantic_sdk_failure_closes_original_hold_without_decision(operation_store, tmp_path, monkeypatch, kind):
    original = (
        OpenAIError("private semantic provider failure")
        if kind == "provider_failure"
        else TimeoutError("private semantic timeout")
        if kind == "provider_timeout"
        else asyncio.CancelledError("private semantic cancellation")
    )
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        calls = []

        async def completion(**_kwargs):
            calls.append(1)
            raise original

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await run_continuation_semantic_advisor(_phases(case), case["runtime"].source.selected)
        assert type(result) is SdkTerminalResult and result.family is SdkTerminalFamily.SEMANTIC
        assert calls == [1]
        receipt = result.verified.receipt
        assert receipt.observation == kind and receipt.producer == "semantic_advisor"
        assert receipt.sdk_entered is True and receipt.sdk_result_observed is False and receipt.retryable is False
        assert receipt.cost.status == "unknown" and receipt.cost.amount is None and receipt.usage.status == "unavailable"
        assert result.deferred_cancellations == ((original,) if kind == "no_observed_result" else ())
        identity = receipt.identity
        with store[0].connect() as conn:
            cold = read_v4_sdk_terminal_verified_on_connection(
                conn,
                session_id=store[4],
                operation_id=identity.operation_id,
                effect_key=identity.effect_key,
                family=SdkTerminalFamily.SEMANTIC,
            )
            attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == identity.attempt_id)).one()
            entry = conn.execute(select(ledger).where(ledger.c.entry_id == attempt.ledger_entry_id)).one()
            hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == identity.attempt_id)).one()
            parent = conn.execute(select(jobs).where(jobs.c.operation_id == identity.operation_id)).one()
        assert cold == result.verified
        assert attempt.settled_at is not None and hold.state == "consumed"
        assert entry.source == "composer" and entry.prompt_tokens is None and entry.completion_tokens is None
        assert parent.status == "failed" and "provider_sdk_terminal" in parent.result_json and "private" not in parent.result_json
        root = decode_continuation_snapshot(result.verified.terminal.payload_json)
        assert root.frames[-1].response is None
        assert not root.pending_settlement_ids and not root.pending_owner_results
        with store[0].begin() as conn:
            conn.execute(update(ledger).where(ledger.c.entry_id == entry.entry_id).values(prompt_tokens=0))
            with pytest.raises(AuditIntegrityError, match="quota ledger"):
                read_v4_sdk_terminal_verified_on_connection(
                    conn,
                    session_id=store[4],
                    operation_id=identity.operation_id,
                    effect_key=identity.effect_key,
                    family=SdkTerminalFamily.SEMANTIC,
                )


@pytest.mark.asyncio
async def test_semantic_sdk_audit_companion_is_privately_issued_exceptional_result(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        original = OpenAIError("private semantic failure")
        audit = AuditIntegrityError("owned semantic audit failure")

        async def completion(**_kwargs):
            raise original

        def fail_audit(**_kwargs):
            raise audit

        monkeypatch.setattr(litellm, "acompletion", completion)
        monkeypatch.setattr(semantic_physical, "build_llm_call_record", fail_audit)
        result = await run_continuation_semantic_advisor(_phases(case), case["runtime"].source.selected)
        assert type(result) is SdkTerminalFailure and result.family is SdkTerminalFamily.SEMANTIC
        require_sdk_terminal_failure(result)
        assert isinstance(result.original, BaseExceptionGroup)
        assert result.original.exceptions == (original, audit)
        with pytest.raises(AuditIntegrityError, match="privately issued"):
            require_sdk_terminal_failure(replace(result))
        identity = result.verified.receipt.identity
        with store[0].connect() as conn:
            cold = read_v4_sdk_terminal_verified_on_connection(
                conn,
                session_id=store[4],
                operation_id=identity.operation_id,
                effect_key=identity.effect_key,
                family=SdkTerminalFamily.SEMANTIC,
            )
        assert cold == result.verified and cold.receipt.cost.status == "unknown"


@pytest.mark.asyncio
async def test_semantic_owned_sdk_seam_error_cannot_mint_provider_failure(operation_store, tmp_path, monkeypatch):
    store = _v4_store(operation_store, monkeypatch)
    async with _semantic_case(store, tmp_path, monkeypatch) as case:
        original = AuditIntegrityError("owned semantic request-integrity failure")

        async def completion(**_kwargs):
            raise original

        monkeypatch.setattr(litellm, "acompletion", completion)
        with pytest.raises(AuditIntegrityError) as caught:
            await run_continuation_semantic_advisor(_phases(case), case["runtime"].source.selected)
        assert caught.value is original
        with store[0].connect() as conn:
            hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
            effect = conn.execute(select(effects).where(effects.c.provider_attempt_id == hold.provider_attempt_id)).one()
            attempt = conn.execute(select(attempts).where(attempts.c.attempt_id == hold.provider_attempt_id)).one()
            cold = read_v4_sdk_terminal_verified_on_connection(
                conn,
                session_id=store[4],
                operation_id=effect.operation_id,
                effect_key=effect.effect_key,
                family=SdkTerminalFamily.SEMANTIC,
            )
        assert cold is None and hold.state == "active" and effect.disposition == "dispatched"
        assert attempt.settled_at is None and attempt.ledger_entry_id is None
