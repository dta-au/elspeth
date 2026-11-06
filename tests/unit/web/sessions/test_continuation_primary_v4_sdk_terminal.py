"""Actual PRIMARY SDK failures, original custody, atomic terminal and cold proof."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import litellm
import pytest
from openai import OpenAIError
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.provider_sdk_terminal import require_sdk_terminal_failure
from elspeth.web.sessions import continuation_primary_sdk_finalization as finalization
from elspeth.web.sessions import continuation_sdk_finalization as shared_finalization
from elspeth.web.sessions.continuation_primary_sdk_terminal import read_v4_primary_sdk_terminal_verified_on_connection
from elspeth.web.sessions.continuation_sdk_terminal_result import PrimarySdkTerminal, SdkTerminalFailure
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from tests.unit.web.sessions import test_continuation_primary_v4_live
from tests.unit.web.sessions.test_continuation_primary_result import primary_input
from tests.unit.web.sessions.test_continuation_primary_v4_incomplete import _invoke

operation_store = test_continuation_primary_v4_live.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider_failure", "provider_timeout", "no_observed_result"])
async def test_actual_sdk_failure_closes_original_attempt_hold_and_cold_terminal(operation_store, tmp_path, monkeypatch, kind):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    original = {
        "provider_failure": OpenAIError("private provider failure text"),
        "provider_timeout": TimeoutError("private timeout text"),
        "no_observed_result": asyncio.CancelledError("private cancellation text"),
    }[kind]
    calls = []

    async def completion(**_kwargs):
        calls.append(1)
        raise original

    monkeypatch.setattr(litellm, "acompletion", completion)
    result = await _invoke(kwargs, produced, recorder, store)
    assert type(result) is PrimarySdkTerminal and calls == [1]
    assert result.receipt.observation == kind
    assert result.receipt.sdk_entered is True and result.receipt.sdk_result_observed is False
    assert result.receipt.retryable is False and result.receipt.cost.status == "unknown"
    assert result.receipt.cost.amount is None and result.receipt.usage.status == "unavailable"
    assert result.deferred_cancellations == ((original,) if kind == "no_observed_result" else ())
    identity = result.receipt.identity
    with store[0].connect() as conn:
        cold = read_v4_primary_sdk_terminal_verified_on_connection(
            conn, session_id=store[4], operation_id=identity.operation_id, effect_key=identity.effect_key
        )
        attempts = conn.execute(select(physical).where(physical.c.session_id == str(store[4]))).all()
        entries = conn.execute(select(ledger).where(ledger.c.session_id == str(store[4]))).all()
        hold = conn.execute(select(holds).where(holds.c.operation_id == identity.operation_id)).one()
        parent = conn.execute(select(jobs).where(jobs.c.operation_id == identity.operation_id)).one()
    assert cold is not None and cold.receipt == result.receipt and cold.terminal == result.snapshot
    assert len(attempts) == len(entries) == 1
    assert hold.state == "consumed" and attempts[0].settled_at is not None
    assert attempts[0].ledger_entry_id == entries[0].entry_id
    assert entries[0].prompt_tokens is None and entries[0].completion_tokens is None
    assert parent.status == "failed" and "provider_sdk_terminal" in parent.result_json
    assert "private" not in parent.result_json
    with store[0].begin() as conn:
        conn.execute(update(ledger).where(ledger.c.entry_id == entries[0].entry_id).values(prompt_tokens=0))
        with pytest.raises(AuditIntegrityError, match="quota ledger"):
            read_v4_primary_sdk_terminal_verified_on_connection(
                conn, session_id=store[4], operation_id=identity.operation_id, effect_key=identity.effect_key
            )


@pytest.mark.asyncio
async def test_sdk_terminal_failure_after_parent_updates_rolls_back_all_disposition(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    calls = []

    async def completion(**_kwargs):
        calls.append(1)
        raise OpenAIError("provider unavailable")

    def fail_settlement(*_args, **_kwargs):
        raise AuditIntegrityError("injected SDK ledger failure")

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(shared_finalization, "settle_provider_attempt_on_connection", fail_settlement)
    with pytest.raises(AuditIntegrityError, match="injected SDK ledger failure"):
        await _invoke(kwargs, produced, recorder, store)
    with store[0].connect() as conn:
        effect = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
        entries = conn.execute(select(ledger).where(ledger.c.session_id == str(store[4]))).all()
        cold = read_v4_primary_sdk_terminal_verified_on_connection(
            conn, session_id=store[4], operation_id=effect.operation_id, effect_key=effect.effect_key
        )
    assert calls == [1] and cold is None
    assert effect.disposition == "dispatched" and effect.receipt_key is None and effect.result_chunk_key is None
    assert hold.state == "active" and attempt.settled_at is None and attempt.ledger_entry_id is None
    assert entries == []


@pytest.mark.asyncio
async def test_copied_original_sdk_witness_cannot_authorize_terminal_writer(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    actual = finalization.finalize_v4_primary_sdk_terminal_on_connection

    async def completion(**_kwargs):
        raise OpenAIError("provider unavailable")

    def copied_writer(conn, *, observation, **arguments):
        return actual(conn, observation=replace(observation), **arguments)

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(finalization, "finalize_v4_primary_sdk_terminal_on_connection", copied_writer)
    with pytest.raises(AuditIntegrityError, match="original issued custody"):
        await _invoke(kwargs, produced, recorder, store)
    with store[0].connect() as conn:
        hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
    assert hold.state == "active" and attempt.settled_at is None


@pytest.mark.asyncio
async def test_arbitrary_owned_failure_is_not_minted_as_sdk_provider_failure(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    original = AuditIntegrityError("owned failure inside instrumented SDK seam")

    async def completion(**_kwargs):
        raise original

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(AuditIntegrityError) as caught:
        await _invoke(kwargs, produced, recorder, store)
    assert caught.value is original
    with store[0].connect() as conn:
        hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
        entries = conn.execute(select(ledger).where(ledger.c.session_id == str(store[4]))).all()
    assert hold.state == "active" and attempt.settled_at is None and entries == []


@pytest.mark.asyncio
async def test_original_sdk_error_survives_audit_failure_while_truthful_terminal_closes(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    sdk_failure = OpenAIError("private provider failure")
    audit_failure = AuditIntegrityError("owned audit builder failure")

    async def completion(**_kwargs):
        raise sdk_failure

    def fail_audit(**_kwargs):
        raise audit_failure

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(provider_gateway, "build_llm_call_record", fail_audit)
    result = await _invoke(kwargs, produced, recorder, store)
    assert type(result) is SdkTerminalFailure
    require_sdk_terminal_failure(result)
    assert isinstance(result.original, BaseExceptionGroup)
    assert result.original.exceptions == (sdk_failure, audit_failure)
    with pytest.raises(AuditIntegrityError, match="privately issued"):
        require_sdk_terminal_failure(replace(result))
    with pytest.raises(AuditIntegrityError, match="privately issued"):
        require_sdk_terminal_failure(replace(result, original=AuditIntegrityError("foreign companion")))
    with store[0].connect() as conn:
        effect = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).one()
        cold = read_v4_primary_sdk_terminal_verified_on_connection(
            conn, session_id=store[4], operation_id=effect.operation_id, effect_key=effect.effect_key
        )
    assert cold is not None and cold.hold_state == "consumed"
    assert cold.receipt.cost.status == "unknown" and cold.receipt.usage.status == "unavailable"


@pytest.mark.asyncio
async def test_current_primary_reservation_covers_sdk_tail_and_rejects_underbound_batch(operation_store, tmp_path, monkeypatch):

    from elspeth.web.sessions.continuation_primary_sdk_tail import build_primary_sdk_tail_batch, prepare_primary_sdk_tail_plan
    from elspeth.web.sessions.continuation_primary_terminal_reservation import primary_terminal_reservation
    from elspeth.web.sessions.time_normalization import restore_utc

    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    actual = finalization.finalize_v4_primary_sdk_terminal_on_connection
    checked = []

    async def completion(**_kwargs):
        raise OpenAIError("provider unavailable")

    def check_original_recipe(conn, *, admission, **arguments):
        paid, no_sdk, incomplete = admission.tail, admission.no_sdk_tail, admission.incomplete_tail
        _, reserved, contract = primary_terminal_reservation(paid, no_sdk)
        sdk_plan = prepare_primary_sdk_tail_plan(identity=paid.identity, admitted=paid.admitted, creator_sequence=paid.creator_sequence)
        assert reserved == max(paid.reserved_bytes, no_sdk.reserved_bytes, incomplete.reserved_bytes, sdk_plan.reserved_bytes)
        assert admission.hold.fallback_contract_sha256 == contract and admission.hold.reserved_bytes == reserved
        result = actual(conn, admission=admission, **arguments)
        plan = prepare_primary_sdk_tail_plan(identity=paid.identity, admitted=paid.admitted, creator_sequence=paid.creator_sequence)
        physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
        with pytest.raises(AuditIntegrityError, match="original closed recipe"):
            build_primary_sdk_tail_batch(
                replace(plan, reserved_bytes=plan.reserved_bytes - 1),
                receipt=result.receipt,
                published_at=restore_utc(physical_row.settled_at),
            )
        checked.append(True)
        return result

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(finalization, "finalize_v4_primary_sdk_terminal_on_connection", check_original_recipe)
    result = await _invoke(kwargs, produced, recorder, store)
    assert type(result) is PrimarySdkTerminal and checked == [True]
