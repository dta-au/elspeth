"""Actual V4 TITLE provider success joins paid custody to one SQL settlement."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import delete, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_title_observation as title_observation
from elspeth.web.composer.continuation_codec import measure_continuation_response_size
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_provider_outcomes import measure_physical_audit_call_size
from elspeth.web.composer.continuation_title_provider import (
    TitleNoSdkTerminalResult,
    TitlePaidIncompleteResult,
    run_prepared_title_provider,
)
from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationOwner
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions import continuation_title_no_sdk_terminal as no_sdk_cold
from elspeth.web.sessions import continuation_title_provider as title_provider
from elspeth.web.sessions.continuation_title_no_sdk_terminal import read_v4_title_no_sdk_terminal_on_connection
from elspeth.web.sessions.continuation_title_paid_incomplete_terminal import read_v4_title_paid_incomplete_terminal_on_connection
from elspeth.web.sessions.continuation_title_paid_overflow_terminal import (
    VerifiedTitlePaidOverflowTerminal,
    read_v4_title_paid_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_title_provider import read_title_result_data_on_connection
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_title_provider import raw_reply, title_case

operation_store = operations.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "post_commit_read_fault"),
    (("deadline", False), ("cancelled", False), ("deadline", True)),
)
async def test_actual_v4_title_predispatch_deadline_closes_original_unentered_hold(
    operation_store, tmp_path, monkeypatch, failure_kind, post_commit_read_fault
):
    from elspeth.web.sessions import continuation_title_no_sdk_close as no_sdk_writer

    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, _composer_service, work, prepared, _guard_requests = await title_case(store, tmp_path, monkeypatch)
    original_dispatch = ProviderCallCustody.require_cost_dispatch
    original_failure = (
        TimeoutError("original TITLE deadline expired before SDK entry")
        if failure_kind == "deadline"
        else asyncio.CancelledError("original TITLE cancellation before SDK entry")
    )

    def expire_after_admission(custody):
        if custody.continuation_title_v4_admission is not None and custody.has_v4_unentered_hold():
            raise original_failure
        return original_dispatch(custody)

    monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", expire_after_admission)
    cold_reads = 0
    original_cold_reader = no_sdk_cold.read_v4_title_no_sdk_terminal_on_connection
    # The writer's in-transaction cold checks must remain real. Only the
    # service's separate read after commit is faulted once.
    assert no_sdk_writer.read_v4_title_no_sdk_terminal_on_connection is original_cold_reader

    def fail_one_post_commit_read(*args, **kwargs):
        nonlocal cold_reads
        cold_reads += 1
        if post_commit_read_fault and cold_reads == 1:
            raise RuntimeError("injected after durable zero-use commit")
        return original_cold_reader(*args, **kwargs)

    monkeypatch.setattr(no_sdk_cold, "read_v4_title_no_sdk_terminal_on_connection", fail_one_post_commit_read)

    async def forbidden_sdk(**_kwargs):
        pytest.fail("TITLE SDK entered after predispatch deadline")

    monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
    result = await run_prepared_title_provider(
        prepared=prepared, invocation_owner=ProviderInvocationOwner(service=store[3], required_work=work)
    )
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with store[0].connect() as conn:
        cold = read_v4_title_no_sdk_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
        )
        effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
        physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
        ledger = conn.execute(select(usage).where(usage.c.entry_id == physical.ledger_entry_id)).one()
    assert type(result) is TitleNoSdkTerminalResult and result.verified == cold
    assert cold is not None and cold.reason.value == ("pre_sdk_deadline" if failure_kind == "deadline" else "cancelled_before_sdk")
    assert result.deferred_cancellations == ((original_failure,) if failure_kind == "cancelled" else ())
    assert cold_reads == (2 if post_commit_read_fault else 1)
    assert effect.disposition == "refused" and hold.state == "consumed"
    assert physical.ledger_entry_id is not None
    assert (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens) == (0, 0, 0, 0)
    work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning_bytes", (0, 100_000))
async def test_actual_v4_title_provider_keeps_complete_response_and_one_credited_ledger(
    operation_store, tmp_path, monkeypatch, reasoning_bytes
):
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, _composer_service, work, prepared, _guard_requests = await title_case(store, tmp_path, monkeypatch)
    sdk_calls: list[str] = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs["model"])
        response = raw_reply(kwargs["model"])
        if reasoning_bytes:
            response.choices[0].message.reasoning_content = "r" * reasoning_bytes
        return response

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
        if result is not None:
            _chunk, cold, settled = read_title_result_data_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=operation,
                sequence=running.claim.segment_sequence,
                generation=running.claim.ownership_generation,
                selected=result.selected,
                names=prepared.names,
            )
    assert result is not None and result.selected.cursor == prepared.selected.cursor + 3
    assert len(sdk_calls) == 1 and effect.disposition == "committed"
    assert hold.state == "consumed" and hold.provider_attempt_id == effect.provider_attempt_id
    assert physical.ledger_entry_id is not None and len(ledger) == 1
    assert ledger[0].source == "auto_title" and ledger[0].model == sdk_calls[0]
    assert settled and cold.completion.provider_metadata.reasoning_content == (
        "r" * reasoning_bytes if reasoning_bytes else "Keep the original café topic."
    )


@pytest.mark.asyncio
async def test_actual_v4_title_oversized_paid_response_returns_cold_terminal_without_second_sdk(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, _composer_service, work, prepared, _guard_requests = await title_case(store, tmp_path, monkeypatch)
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
    assert len(observed_sizes) == 2 and observed_sizes[0] == observed_sizes[1]
    assert observed_sizes[0][0] is not None and observed_sizes[0][1]
    assert (result.receipt.reason, result.receipt.representation, result.receipt.evidence_status) == (
        "private_document_bytes",
        "response",
        "incomplete",
    )
    assert result.receipt.retryable is False and result.receipt.observed_bytes == 1_048_577
    assert len(sdk_calls) == 1 and effect.disposition == "paid_overflow"
    assert hold.state == "consumed" and len(ledger) == 1 and ledger[0].model == sdk_calls[0]


@pytest.mark.asyncio
async def test_actual_v4_title_returned_incomplete_closes_original_paid_attempt(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, _composer_service, work, prepared, _guard_requests = await title_case(store, tmp_path, monkeypatch)
    sdk_calls: list[str] = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs["model"])
        return ModelResponse(
            id="returned-incomplete-title",
            model=kwargs["model"],
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Title from actual provider",
                        "reasoning_details": [{"text": "r" * 200_000} for _ in range(7)],
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
        cold = read_v4_title_paid_incomplete_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
        )
        parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
        effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
        physical = conn.execute(select(attempts).where(attempts.c.attempt_id == effect.provider_attempt_id)).one()
        ledger = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
    assert type(result) is TitlePaidIncompleteResult and result.verified == cold
    assert result.verified.receipt.reason == "capture_budget_exceeded"
    assert result.verified.receipt.retryable is False and result.verified.receipt.evidence_status == "incomplete"
    assert len(sdk_calls) == 1 and effect.disposition == "paid_incomplete"
    assert (
        parent.status == "failed" and parent.failure_code == "http_error" and '"error_type":"paid_result_incomplete"' in parent.result_json
    )
    assert hold.state == "consumed" and physical.ledger_entry_id is not None
    assert len(ledger) == 1 and ledger[0].model == sdk_calls[0]
    work.coordinator.assert_completed()

    original_bootstrap_values = title_provider.bootstrap_values

    def changed_bootstrap_request(snapshot):
        values = original_bootstrap_values(snapshot)
        assert type(values.request) is SendMessageRequest
        return replace(values, request=values.request.model_copy(update={"content": values.request.content + " altered"}))

    with monkeypatch.context() as patch:
        patch.setattr(title_provider, "bootstrap_values", changed_bootstrap_request)
        with store[0].connect() as conn, pytest.raises(AuditIntegrityError, match="original logical request/actor/eligibility"):
            read_v4_title_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
            )

    with store[0].connect() as conn:
        public_rows = conn.execute(
            select(messages).where(
                messages.c.session_id == sid,
                messages.c.role == "audit",
                messages.c.content == "Provider call result recorded.",
            )
        ).all()
        assert len(public_rows) == 1
        trigger = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_chat_messages_no_delete'"
        ).scalar_one()
        deleted = conn.begin_nested()
        conn.exec_driver_sql("DROP TRIGGER trg_chat_messages_no_delete")
        conn.execute(delete(messages).where(messages.c.id == public_rows[0].id))
        with pytest.raises(AuditIntegrityError, match="one bounded original public call"):
            read_v4_title_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
            )
        deleted.rollback()
        assert (
            conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_chat_messages_no_delete'").scalar_one()
            == trigger
        )
        altered = deepcopy(public_rows[0].tool_calls)
        altered[0]["call"]["call_id"] = "00000000-0000-0000-0000-000000000000"
        changed = conn.begin_nested()
        conn.execute(update(messages).where(messages.c.id == public_rows[0].id).values(tool_calls=altered))
        with pytest.raises(AuditIntegrityError):
            read_v4_title_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
            )
        changed.rollback()
        assert (
            read_v4_title_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
            )
            == cold
        )
