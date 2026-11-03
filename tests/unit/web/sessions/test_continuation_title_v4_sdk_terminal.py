"""TITLE SDK failures use the actual gateway, original paid source and cold proof."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import litellm
import pytest
from openai import OpenAIError
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_title_provider as driver
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.provider_sdk_terminal import require_sdk_terminal_failure
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions import continuation_sdk_finalization as finalization
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
from elspeth.web.sessions.continuation_sdk_terminal_cold import read_v4_sdk_terminal_verified_on_connection
from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, SdkTerminalResult
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_title_provider import raw_reply
from tests.unit.web.sessions.test_continuation_v3_title_paid_tail_preflight import _historical_prepared_title, _title_owner

operation_store = operations.operation_store


async def _v4_title_input(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, _composer, work, prepared, _guard_requests = await _historical_prepared_title(store, policy, tmp_path, monkeypatch)
    with store[0].connect() as conn:
        original_title = conn.execute(select(sessions.c.title).where(sessions.c.id == str(store[4]))).scalar_one()
    return store, running, prepared, _title_owner(store, work, prepared), original_title


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("provider_failure", "no_observed_result"))
async def test_actual_title_sdk_failure_closes_only_original_paid_attempt_and_cold_terminal(operation_store, tmp_path, monkeypatch, kind):
    store, running, prepared, owner, original_title = await _v4_title_input(operation_store, tmp_path, monkeypatch)
    original = (
        OpenAIError("private TITLE provider failure") if kind == "provider_failure" else asyncio.CancelledError("private TITLE cancel")
    )
    calls = []

    async def completion(**_kwargs):
        calls.append(1)
        raise original

    monkeypatch.setattr(litellm, "acompletion", completion)
    result = await driver.run_prepared_title_provider(prepared=prepared, invocation_owner=owner)
    assert type(result) is SdkTerminalResult and result.family is SdkTerminalFamily.TITLE and calls == [1]
    assert result.verified.receipt.observation == kind
    assert result.verified.receipt.sdk_entered is True and result.verified.receipt.sdk_result_observed is False
    assert result.verified.receipt.retryable is False
    assert result.verified.receipt.cost.status == "unknown" and result.verified.receipt.cost.amount is None
    assert result.verified.receipt.usage.status == "unavailable"
    assert result.deferred_cancellations == ((original,) if kind == "no_observed_result" else ())
    identity = result.verified.receipt.identity
    with store[0].connect() as conn:
        cold = read_v4_sdk_terminal_verified_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=identity.operation_id,
            effect_key="provider:3:0",
            family=SdkTerminalFamily.TITLE,
            names=prepared.names,
        )
        attempts = conn.execute(select(physical).where(physical.c.session_id == str(store[4]))).all()
        entries = conn.execute(select(ledger).where(ledger.c.session_id == str(store[4]))).all()
        hold = conn.execute(select(holds).where(holds.c.operation_id == identity.operation_id)).one()
        effect = conn.execute(select(effects).where(effects.c.operation_id == identity.operation_id)).one()
        parent = conn.execute(select(jobs).where(jobs.c.operation_id == identity.operation_id)).one()
        title_after = conn.execute(select(sessions.c.title).where(sessions.c.id == str(store[4]))).scalar_one()
    assert cold == result.verified and cold is not None and cold.hold_state == "consumed"
    assert len(attempts) == len(entries) == 1 and entries[0].source == "auto_title" and entries[0].run_id is None
    assert hold.state == "consumed" and hold.provider_attempt_id == identity.attempt_id
    assert attempts[0].settled_at is not None and attempts[0].ledger_entry_id == entries[0].entry_id
    assert effect.disposition == "sdk_terminal" and effect.provider_attempt_id == identity.attempt_id
    assert parent.status == "failed" and "provider_sdk_terminal" in parent.result_json and "private" not in parent.result_json
    assert title_after == original_title
    with store[0].begin() as conn:
        conn.execute(update(ledger).where(ledger.c.entry_id == entries[0].entry_id).values(prompt_tokens=0))
        with pytest.raises(AuditIntegrityError, match="quota ledger"):
            read_v4_sdk_terminal_verified_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=identity.operation_id,
                effect_key="provider:3:0",
                family=SdkTerminalFamily.TITLE,
                names=prepared.names,
            )


@pytest.mark.asyncio
async def test_copied_sdk_failure_witness_cannot_close_title_or_apply_it(operation_store, tmp_path, monkeypatch):
    store, _running, prepared, owner, original_title = await _v4_title_input(operation_store, tmp_path, monkeypatch)
    actual = finalization.require_sdk_live_source_on_connection
    calls = []

    async def completion(**_kwargs):
        calls.append(1)
        raise OpenAIError("original TITLE provider unavailable")

    def copied_source(conn, *, custody, observation):
        return actual(conn, custody=custody, observation=replace(observation))

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(finalization, "require_sdk_live_source_on_connection", copied_source)
    with pytest.raises(AuditIntegrityError, match="original issued custody"):
        await driver.run_prepared_title_provider(prepared=prepared, invocation_owner=owner)
    with store[0].connect() as conn:
        hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
        entries = conn.execute(select(ledger).where(ledger.c.session_id == str(store[4]))).all()
        effect = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).one()
        title_after = conn.execute(select(sessions.c.title).where(sessions.c.id == str(store[4]))).scalar_one()
    assert calls == [1] and hold.state == "active" and attempt.settled_at is None and entries == []
    assert effect.disposition == "dispatched" and title_after == original_title


@pytest.mark.asyncio
async def test_original_title_sdk_and_audit_companion_closes_paid_attempt_without_full_call(operation_store, tmp_path, monkeypatch):
    store, running, prepared, owner, original_title = await _v4_title_input(operation_store, tmp_path, monkeypatch)
    sdk_error = OpenAIError("private original TITLE SDK failure")
    audit_error = AuditIntegrityError("private original TITLE audit failure")
    calls = []

    async def completion(**_kwargs):
        calls.append(1)
        raise sdk_error

    def fail_audit(**_kwargs):
        raise audit_error

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(_auto_title, "build_llm_call_record", fail_audit)
    result = await driver.run_prepared_title_provider(prepared=prepared, invocation_owner=owner)
    assert type(result) is SdkTerminalFailure and result.family is SdkTerminalFamily.TITLE and calls == [1]
    require_sdk_terminal_failure(result)
    assert isinstance(result.original, BaseExceptionGroup)
    assert result.original.exceptions == (sdk_error, audit_error)
    assert result.verified.receipt.cost.status == "unknown" and result.verified.receipt.cost.amount is None
    assert result.verified.receipt.usage.status == "unavailable"
    with store[0].connect() as conn:
        cold = read_v4_sdk_terminal_verified_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=running.claim.operation_id,
            effect_key="provider:3:0",
            family=SdkTerminalFamily.TITLE,
            names=prepared.names,
        )
        attempts = conn.execute(select(physical).where(physical.c.session_id == str(store[4]))).all()
        entries = conn.execute(select(ledger).where(ledger.c.session_id == str(store[4]))).all()
        hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
        title_after = conn.execute(select(sessions.c.title).where(sessions.c.id == str(store[4]))).scalar_one()
    assert cold == result.verified and hold.state == "consumed"
    assert len(attempts) == len(entries) == 1 and entries[0].source == "auto_title"
    assert attempts[0].ledger_entry_id == entries[0].entry_id and attempts[0].settled_at is not None
    assert title_after == original_title


@pytest.mark.asyncio
async def test_raw_title_sdk_transport_closes_original_paid_attempt(operation_store, tmp_path, monkeypatch):
    store, running, prepared, owner, original_title = await _v4_title_input(operation_store, tmp_path, monkeypatch)
    raw_transport = httpx.TransportError("private original TITLE transport")
    calls = []

    async def fail_in_sdk(**_kwargs):
        calls.append(1)
        raise raw_transport

    monkeypatch.setattr(litellm, "acompletion", fail_in_sdk)
    result = await driver.run_prepared_title_provider(prepared=prepared, invocation_owner=owner)
    assert type(result) is SdkTerminalFailure and result.family is SdkTerminalFamily.TITLE and calls == [1]
    require_sdk_terminal_failure(result)
    assert type(result.original).__name__ == "_AutoTitleProviderTransportError"
    assert result.original.__context__ is raw_transport
    assert result.verified.receipt.observation == "provider_failure"
    with store[0].connect() as conn:
        cold = read_v4_sdk_terminal_verified_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=running.claim.operation_id,
            effect_key="provider:3:0",
            family=SdkTerminalFamily.TITLE,
            names=prepared.names,
        )
        title_after = conn.execute(select(sessions.c.title).where(sessions.c.id == str(store[4]))).scalar_one()
    assert cold == result.verified and title_after == original_title


@pytest.mark.asyncio
async def test_transport_shaped_audit_failure_after_sdk_return_has_no_sdk_authority(operation_store, tmp_path, monkeypatch):
    store, _running, prepared, owner, original_title = await _v4_title_input(operation_store, tmp_path, monkeypatch)
    post_return = httpx.TransportError("audit-only TITLE transport-shaped failure")
    returned = []

    async def return_from_sdk(**kwargs):
        returned.append(1)
        return raw_reply(kwargs["model"])

    def fail_after_return(**_kwargs):
        raise post_return

    monkeypatch.setattr(litellm, "acompletion", return_from_sdk)
    monkeypatch.setattr(_auto_title, "build_llm_call_record", fail_after_return)
    with pytest.raises(httpx.TransportError) as caught:
        await driver.run_prepared_title_provider(prepared=prepared, invocation_owner=owner)
    assert caught.value is post_return and returned == [1]
    with store[0].connect() as conn:
        hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
        entries = conn.execute(select(ledger).where(ledger.c.session_id == str(store[4]))).all()
        effect = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).one()
        title_after = conn.execute(select(sessions.c.title).where(sessions.c.id == str(store[4]))).scalar_one()
    assert hold.state == "active" and attempt.settled_at is None and entries == []
    assert effect.disposition == "dispatched" and title_after == original_title
