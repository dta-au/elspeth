"""Actual PRIMARY returned-incomplete terminal and rollback controls."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_audit_source import ProviderAuditSource
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete
from elspeth.web.composer.visible_tool_event import visible_response_gap_for_source
from elspeth.web.sessions import continuation_primary_paid_incomplete_finalization as finalization
from elspeth.web.sessions.continuation_cost import _closed_call
from elspeth.web.sessions.continuation_primary import PrimaryPaidIncompleteTerminal
from elspeth.web.sessions.continuation_primary_paid_incomplete_terminal import read_v4_primary_paid_incomplete_verified_on_connection
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc
from tests.unit.web.sessions import test_continuation_primary_v4_live
from tests.unit.web.sessions.test_continuation_primary_result import primary_input
from tests.unit.web.sessions.test_continuation_prompt_producers import REQUEST

operation_store = test_continuation_primary_v4_live.operation_store


async def _invoke(kwargs, produced, recorder, store):
    with prompt_guard_user_scope(REQUEST):
        return await run_continuation_primary(
            service=kwargs["service"],
            authority=kwargs["authority"],
            selected=kwargs["selected"],
            producers=produced.producers,
            messages=list(produced.messages),
            tools=[],
            recorder=recorder,
            drained=RecorderDrainCursors(),
            required_work=kwargs["required_work"],
            provider_owner=ProviderInvocationOwner(service=store[3], required_work=kwargs["required_work"]),
            timeout=5,
        )


def _incomplete_response():
    # The SDK really returns. The bounded original-field parser cannot retain
    # the unsupported nested value; no valid output is substituted for it.
    response = ModelResponse(
        model="test/model",
        choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": "must not apply", "tool_calls": None}}],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
    )
    response.choices[0].message.reasoning_details = [{"unsupported": object()}]
    return response


@pytest.mark.asyncio
async def test_primary_actual_incomplete_return_settles_once_and_cold_rejects_ledger_mutation(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    sdk = []

    async def completion(**_kwargs):
        sdk.append(1)
        return _incomplete_response()

    monkeypatch.setattr(litellm, "acompletion", completion)
    result = await _invoke(kwargs, produced, recorder, store)
    assert type(result) is PrimaryPaidIncompleteTerminal and sdk == [1]
    assert result.receipt.reason == "unsupported_field" and result.receipt.capture_surface == "provider_metadata"
    assert result.receipt.cost.status == "known" and result.receipt.cost.amount == "0.01"
    assert result.receipt.retryable is False and result.receipt.provider_returned is True
    identity = result.receipt.identity
    with store[0].connect() as conn:
        cold = read_v4_primary_paid_incomplete_verified_on_connection(
            conn, session_id=store[4], operation_id=identity.operation_id, effect_key=identity.effect_key
        )
        assert cold is not None and cold.receipt == result.receipt and cold.terminal == result.snapshot
        attempts = conn.execute(select(physical).where(physical.c.session_id == str(store[4]))).all()
        entries = conn.execute(select(ledger).where(ledger.c.session_id == str(store[4]))).all()
        parent = conn.execute(select(jobs).where(jobs.c.operation_id == identity.operation_id)).one()
        hold = conn.execute(select(holds).where(holds.c.operation_id == identity.operation_id)).one()
    assert len(attempts) == len(entries) == 1
    assert attempts[0].ledger_entry_id == entries[0].entry_id and attempts[0].settled_at is not None
    assert hold.state == "consumed" and parent.status == "failed"
    assert "paid_result_incomplete" in parent.result_json and "must not apply" not in parent.result_json
    with store[0].begin() as conn:
        conn.execute(update(ledger).where(ledger.c.entry_id == entries[0].entry_id).values(prompt_tokens=999))
        with pytest.raises(AuditIntegrityError, match="quota ledger"):
            read_v4_primary_paid_incomplete_verified_on_connection(
                conn, session_id=store[4], operation_id=identity.operation_id, effect_key=identity.effect_key
            )


@pytest.mark.asyncio
async def test_primary_incomplete_sql_failure_rolls_back_original_effect_hold_and_ledger(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    sdk = []

    async def completion(**_kwargs):
        sdk.append(1)
        return _incomplete_response()

    def fail_after_terminal_rows(*_args, **_kwargs):
        raise AuditIntegrityError("injected incomplete settlement failure")

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(finalization, "settle_provider_attempt_on_connection", fail_after_terminal_rows)
    with pytest.raises(BaseException, match="injected incomplete settlement failure"):
        await _invoke(kwargs, produced, recorder, store)
    with store[0].connect() as conn:
        effect = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
        entries = conn.execute(select(ledger).where(ledger.c.session_id == str(store[4]))).all()
    assert sdk == [1] and effect.disposition == "dispatched"
    assert effect.receipt_key is None and effect.result_chunk_key is None
    assert hold.state == "active" and attempt.settled_at is None and attempt.ledger_entry_id is None
    assert entries == []
    with store[0].connect() as conn:
        public_audits = conn.execute(
            select(messages.c.id).where(messages.c.session_id == str(store[4]), messages.c.content == "Provider call result recorded.")
        ).all()
    assert public_audits == []


@pytest.mark.asyncio
async def test_primary_direct_writer_rejects_unissued_copy_of_actual_incomplete_result(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    actual_writer = finalization.finalize_v4_primary_incomplete_on_connection
    sdk_calls = []

    async def completion(**_kwargs):
        sdk_calls.append(1)
        return _incomplete_response()

    def substitute_equal_fields(conn, *, session_service, admission, prepared, incomplete, call):
        copied = V4ProviderResultIncomplete(sdk_return=incomplete.sdk_return, metadata=incomplete.metadata, losses=incomplete.losses)
        assert copied is not incomplete
        return actual_writer(conn, session_service=session_service, admission=admission, prepared=prepared, incomplete=copied, call=call)

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(finalization, "finalize_v4_primary_incomplete_on_connection", substitute_equal_fields)
    with pytest.raises(AuditIntegrityError, match="original parser issuance"):
        await _invoke(kwargs, produced, recorder, store)
    with store[0].connect() as conn:
        effect = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
        entries = conn.execute(select(ledger).where(ledger.c.session_id == str(store[4]))).all()
    assert sdk_calls == [1] and effect.disposition == "dispatched"
    assert hold.state == "active" and attempt.settled_at is None and entries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("delete", "companion", "cost", "usage", "finish_next_second"))
async def test_primary_incomplete_cold_requires_original_public_audit(operation_store, tmp_path, monkeypatch, changed):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store

    async def completion(**_kwargs):
        return _incomplete_response()

    monkeypatch.setattr(litellm, "acompletion", completion)
    result = await _invoke(kwargs, produced, recorder, store)
    assert type(result) is PrimaryPaidIncompleteTerminal
    identity = result.receipt.identity
    with store[0].begin() as conn:
        cold = read_v4_primary_paid_incomplete_verified_on_connection(
            conn, session_id=store[4], operation_id=identity.operation_id, effect_key=identity.effect_key
        )
        assert cold is not None and cold.receipt == result.receipt
        row = conn.execute(
            select(messages).where(
                messages.c.session_id == str(store[4]),
                messages.c.role == "audit",
                messages.c.content == "Provider call result recorded.",
            )
        ).one()
        if changed == "delete":
            assert conn.dialect.name == "sqlite"
            original_trigger = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_chat_messages_no_delete'"
            ).scalar_one()
            savepoint = conn.begin_nested()
            try:
                # Simulate storage corruption only inside this disposable
                # savepoint; ordinary application DELETE remains forbidden.
                conn.exec_driver_sql("DROP TRIGGER trg_chat_messages_no_delete")
                conn.execute(delete(messages).where(messages.c.id == row.id))
                with pytest.raises(AuditIntegrityError, match="one bounded original public row"):
                    read_v4_primary_paid_incomplete_verified_on_connection(
                        conn, session_id=store[4], operation_id=identity.operation_id, effect_key=identity.effect_key
                    )
            finally:
                savepoint.rollback()
            assert (
                conn.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_chat_messages_no_delete'"
                ).scalar_one()
                == original_trigger
            )
            assert (
                read_v4_primary_paid_incomplete_verified_on_connection(
                    conn, session_id=store[4], operation_id=identity.operation_id, effect_key=identity.effect_key
                )
                == cold
            )
            with pytest.raises(IntegrityError, match="chat_messages rows are append-only"):
                conn.execute(delete(messages).where(messages.c.id == row.id))
            return
        else:
            calls = deepcopy(row.tool_calls)
            if changed == "companion":
                calls[1]["operation_id"] = "foreign-operation"
            elif changed == "cost":
                calls[0]["call"]["provider_cost"] = 0.02
            elif changed == "finish_next_second":
                source = ProviderAuditSource(identity.session_id, identity.session_operation_id, identity.session_operation_epoch)
                original = _closed_call(row, source, identity.attempt_id)
                observed_at = conn.execute(
                    select(effects.c.response_finished_at).where(
                        effects.c.session_id == identity.session_id,
                        effects.c.operation_id == identity.operation_id,
                        effects.c.effect_key == identity.effect_key,
                    )
                ).scalar_one()
                forged = replace(original, finished_at=restore_utc(observed_at) + timedelta(seconds=1, microseconds=1))
                envelope = llm_call_audit_envelope(forged)
                # Recompute the companion so this negative reaches the actual
                # physical time fence, not merely its public-envelope digest.
                calls = [envelope, visible_response_gap_for_source(forged, source, envelope)]
            else:
                calls[0]["call"]["prompt_tokens"] = 999
            conn.execute(update(messages).where(messages.c.id == row.id).values(tool_calls=calls))
        with pytest.raises(AuditIntegrityError, match="physical fence" if changed == "finish_next_second" else None):
            read_v4_primary_paid_incomplete_verified_on_connection(
                conn, session_id=store[4], operation_id=identity.operation_id, effect_key=identity.effect_key
            )
