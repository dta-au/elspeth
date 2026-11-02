"""Original TITLE admission can close zero-use, with a cold corruption proof."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_title_no_sdk import TitleNoSdkIssuer, TitleNoSdkRefusalReason
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_title_no_sdk_close import refuse_v4_title_no_sdk_on_connection
from elspeth.web.sessions.continuation_title_no_sdk_terminal import read_v4_title_no_sdk_terminal_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_v3_title_paid_tail_preflight import _historical_prepared_title

operation_store = operations.operation_store


async def _admitted_title(operation_store, tmp_path, monkeypatch):
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
    custody = ProviderInvocationOwner(service=store[3], required_work=work).mint(ProviderInvocationFamily.TITLE)
    custody.bind_continuation_title(prepared)
    issuer = TitleNoSdkIssuer(prepared)
    custody.bind_title_no_sdk_issuer(issuer)
    await custody.admit(model=prepared.request.kwargs["model"])
    assert custody.continuation_title_v4_admission is not None
    return store, running, prepared, custody, issuer


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", (TitleNoSdkRefusalReason.PRE_SDK_DEADLINE, TitleNoSdkRefusalReason.CANCELLED_BEFORE_SDK))
async def test_v4_title_zero_use_terminal_binds_original_admission_and_cold_ledger(operation_store, tmp_path, monkeypatch, reason):
    store, running, prepared, custody, issuer = await _admitted_title(operation_store, tmp_path, monkeypatch)
    failure = TimeoutError("original pre-SDK expiry") if reason is TitleNoSdkRefusalReason.PRE_SDK_DEADLINE else asyncio.CancelledError()
    issuer.note_original_failure(custody, failure)
    permit = issuer.reserve_refusal(reason=reason, original_failure=failure)
    with pytest.raises(AuditIntegrityError, match="foreign or copied"):
        issuer.require_refusal_permit(replace(permit))
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(store[0], sid) as conn:
        record = refuse_v4_title_no_sdk_on_connection(conn, service=store[3], permit=permit)
    with store[0].connect() as conn:
        verified = read_v4_title_no_sdk_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
        )
        hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
        ledger = conn.execute(select(usage).where(usage.c.entry_id == attempt.ledger_entry_id)).one()
        parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    assert verified is not None and verified.reason is reason and verified.hold_state == "consumed"
    assert record.status == "failed" and parent.status == "failed"
    assert parent.result_json is not None and '"retryable":false' in parent.result_json
    assert verified.provider_attempt_id == custody.attempt.attempt_id
    assert verified.terminal.checkpoint_id == record.checkpoint_id
    assert verified.terminal.cursor == record.journal_cursor
    assert hold.state == "consumed" and attempt.settled_at is not None
    assert (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens) == (0, 0, 0, 0)
    with store[0].connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(update(usage).where(usage.c.entry_id == attempt.ledger_entry_id).values(prompt_tokens=1))
            with pytest.raises(AuditIntegrityError, match="zero-use audit and ledger"):
                read_v4_title_no_sdk_terminal_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
                )
        finally:
            transaction.rollback()


@pytest.mark.asyncio
async def test_title_issuer_never_certifies_an_entered_sdk(operation_store, tmp_path, monkeypatch):
    _store, _running, _prepared, custody, issuer = await _admitted_title(operation_store, tmp_path, monkeypatch)
    custody.mark_sdk_entered()
    failure = TimeoutError("after actual SDK entry")
    issuer.note_original_failure(custody, failure)
    with pytest.raises(AuditIntegrityError, match="original unentered failure"):
        issuer.reserve_refusal(reason=TitleNoSdkRefusalReason.PRE_SDK_DEADLINE, original_failure=failure)
