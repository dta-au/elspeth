"""Real V4 TITLE dispatch and paid/incomplete terminal SQL custody."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_title_observation import TitleOverflowObservation, TitleOverflowUsageObservation
from elspeth.web.composer.provider_audit_source import capture_provider_audit_source
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions import continuation_title_provider as title_provider
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost
from elspeth.web.sessions.continuation_title_paid_overflow_terminal import read_v4_title_paid_overflow_terminal_on_connection
from elspeth.web.sessions.continuation_title_provider import CurrentTitleNames, admit_v4_title_provider_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_v3_title_paid_tail_preflight import _historical_prepared_title

operation_store = operations.operation_store


@pytest.mark.asyncio
async def test_v4_title_paid_overflow_is_one_terminal_and_cold_proved(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, composer_service, _work, prepared, _guard_requests = await _historical_prepared_title(store, policy, tmp_path, monkeypatch)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(store[0], sid) as conn:
        with pytest.raises(AuditIntegrityError, match="actual prepared owner"):
            admit_v4_title_provider_on_connection(conn, composer_service, prepared, model=prepared.request.kwargs["model"])
        admission = admit_v4_title_provider_on_connection(conn, store[3], prepared, model=prepared.request.kwargs["model"])
    observation = TitleOverflowObservation(
        source=capture_provider_audit_source(running.session_operation_context),
        attempt_id=admission.attempt.attempt_id,
        representation="response",
        document_codec="composer.response.v1",
        measurement="at_least",
        observed_bytes=1_048_577,
        limit_bytes=1_048_576,
        cost=PaidOverflowCost(status="unknown", amount=None, source="not_available"),
        usage=TitleOverflowUsageObservation("unavailable", None, None, None, None, None),
    )
    committed = store[3].settle_v4_title_overflow_sync(
        prepared=prepared,
        admission=admission,
        observation=observation,
        names=CurrentTitleNames(running, admission.selected, prepared.names),
    )
    receipt = committed.receipt
    with store[0].connect() as conn:
        verified = read_v4_title_paid_overflow_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
        )
        parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
        effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation)).one()
        hold = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).one()
        physical = conn.execute(select(attempts).where(attempts.c.attempt_id == admission.attempt.attempt_id)).one()
        ledgers = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
    assert verified == committed and verified.receipt == receipt and verified.hold_state == "consumed"
    assert verified.terminal.cursor == admission.selected.cursor + 2
    assert (parent.status, parent.failure_code, parent.journal_cursor) == ("failed", "http_error", verified.terminal.cursor)
    assert parent.result_json is not None and '"error_type":"paid_result_overflow"' in parent.result_json
    assert effect.disposition == "paid_overflow" and effect.provider_attempt_id == admission.attempt.attempt_id
    assert hold.state == "consumed" and hold.provider_attempt_id == admission.attempt.attempt_id
    assert physical.ledger_entry_id is not None and len(ledgers) == 1
    assert ledgers[0].model == prepared.request.kwargs["model"]
    assert all(
        ledgers[0]._mapping[name] is None for name in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens", "reasoning_tokens")
    )

    # Simulate a changed decoded bootstrap request after the settled parent has
    # deliberately cleared request_json. Cold proof must still bind the original
    # request in the immutable bootstrap to the original pre-TITLE frame.
    original_bootstrap_values = title_provider.bootstrap_values

    def changed_bootstrap_request(snapshot):
        values = original_bootstrap_values(snapshot)
        assert type(values.request) is SendMessageRequest
        changed_request = values.request.model_copy(update={"content": values.request.content + " altered"})
        return replace(values, request=changed_request)

    with monkeypatch.context() as patch:
        patch.setattr(title_provider, "bootstrap_values", changed_bootstrap_request)
        with store[0].connect() as conn, pytest.raises(AuditIntegrityError, match="original logical request/actor/eligibility"):
            read_v4_title_paid_overflow_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, names=prepared.names
            )
