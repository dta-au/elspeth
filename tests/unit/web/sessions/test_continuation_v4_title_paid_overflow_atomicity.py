"""V4 TITLE terminal SQL must never commit a paid/incomplete prefix."""

from __future__ import annotations

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.contracts.errors import ComposerOwnedSettlementFailure
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_title_provider import run_prepared_title_provider
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions import continuation_title_paid_overflow_settlement as terminal_writer
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_title_provider import read_v4_title_admission_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_v3_title_paid_tail_preflight import _historical_prepared_title

operation_store = operations.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("injected_stage", ("after_append", "after_quota"))
async def test_v4_title_paid_overflow_rolls_back_every_terminal_prefix(operation_store, tmp_path, monkeypatch, injected_stage):
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
    sid, operation = str(running.claim.session_id), running.claim.operation_id

    async def completion(**kwargs):
        return ModelResponse(
            id="actual-rollback-title",
            model=kwargs["model"],
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "z" * 230_000,
                        "reasoning_details": [{"text": "y" * 60_000} for _ in range(8)],
                    },
                }
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)

    if injected_stage == "after_append":
        original_append = terminal_writer.append_complete_snapshot

        def fail_after_append(*args, **kwargs):
            original_append(*args, **kwargs)
            raise RuntimeError("injected after terminal append")

        monkeypatch.setattr(terminal_writer, "append_complete_snapshot", fail_after_append)
    else:
        original_quota = terminal_writer.settle_provider_attempt_on_connection

        def fail_after_quota(*args, **kwargs):
            original_quota(*args, **kwargs)
            raise RuntimeError("injected after quota settlement")

        monkeypatch.setattr(terminal_writer, "settle_provider_attempt_on_connection", fail_after_quota)

    expected_message = "injected after terminal append" if injected_stage == "after_append" else "injected after quota settlement"
    with pytest.raises(ComposerOwnedSettlementFailure) as failure:
        await run_prepared_title_provider(prepared=prepared, invocation_owner=ProviderInvocationOwner(service=store[3], required_work=work))
    assert type(failure.value.__cause__) is RuntimeError and str(failure.value.__cause__) == expected_message

    with locked_session_transaction(store[0], sid) as conn:
        admission = read_v4_title_admission_on_connection(conn, prepared=prepared)
        assert admission is not None
        parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
        effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
        physical = conn.execute(select(attempts).where(attempts.c.attempt_id == admission.attempt.attempt_id)).one()
        terminal_rows = conn.execute(
            select(checkpoints.c.checkpoint_id).where(
                checkpoints.c.session_id == sid,
                checkpoints.c.operation_id == operation,
                checkpoints.c.cursor > admission.selected.cursor,
            )
        ).all()
        receipt_chunks = conn.execute(
            select(chunks.c.chunk_key).where(
                chunks.c.session_id == sid,
                chunks.c.operation_id == operation,
                chunks.c.codec == "composer.paid-overflow.v1",
            )
        ).all()
        ledgers = conn.execute(select(usage.c.entry_id).where(usage.c.session_id == sid)).all()
        public_rows = conn.execute(
            select(messages.c.id).where(
                messages.c.session_id == sid,
                messages.c.role == "audit",
                messages.c.content == "Provider call result recorded.",
            )
        ).all()
        current = load_snapshot(conn, session_id=sid, operation_id=operation)
    assert current == admission.selected
    assert parent.status == "running" and parent.result_json is None
    assert effect.disposition == "dispatched" and effect.receipt_key is None and effect.result_chunk_key is None
    assert hold.state == "active" and hold.provider_attempt_id == admission.attempt.attempt_id
    assert physical.settled_at is None and physical.ledger_entry_id is None
    assert not terminal_rows and not receipt_chunks and not ledgers and not public_rows
