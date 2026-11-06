"""Actual paid-invalid driver returns preserve postcommit secondary failures."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_continuation_snapshot
from elspeth.web.composer.continuation_semantic_advisor_phase import SemanticAdvisorPaidInvalidTerminal
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions import composer_async_worker as worker
from elspeth.web.sessions import continuation_driver as driver_module
from elspeth.web.sessions import continuation_semantic_advisor as semantic_sql
from elspeth.web.sessions.models import composer_async_operations_table as operations
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ADVISOR_MODEL,
    ScriptedSDK,
    continuation_app,
    effects,
    install_sdk,
    observe_supported_path,
    sdk_reply,
    submit_and_settle,
    tool_call,
)


def _contains(error: BaseException, original: BaseException) -> bool:
    return error is original or (isinstance(error, BaseExceptionGroup) and any(_contains(child, original) for child in error.exceptions))


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("cold_read", "watcher_cleanup"))
async def test_paid_invalid_driver_preserves_postcommit_secondary_failure(tmp_path, monkeypatch, fault):
    operation_id = str(uuid4())
    private_text = "PRIVATE_MP1_PROVIDER_REPLY: complete malformed non-JSON response"
    secondary = RuntimeError("injected postcommit secondary failure")
    returned, injected, joined = [], [], []
    failure_observed = asyncio.Event()
    original_join = SessionOperationLease.join_continuation_stage
    original_cold = driver_module._paid_result_data
    original_cleanup = worker._finish_operation_watcher
    original_failure = driver_module._SegmentDriver.join_failure

    async with continuation_app(tmp_path, monkeypatch) as harness:
        policy = harness.app.state.composer_async_operation_authority._continuation_policy
        assert policy is not None and policy.actions.advisor.preparation == 5.0

        def committed_state():
            assert len(returned) == 1
            paid = returned[0][0].paid
            effect = next(row for row in effects(harness.app, operation_id) if row.effect_key == paid.effect_key)
            with harness.app.state.session_engine.connect() as conn:
                parent = conn.execute(select(operations).where(operations.c.operation_id == operation_id)).one()
                attempt = conn.execute(select(physical).where(physical.c.attempt_id == effect.provider_attempt_id)).one()
                hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == attempt.attempt_id)).one()
                ledger = conn.execute(select(usage).where(usage.c.entry_id == attempt.ledger_entry_id)).one()
            assert parent.status == "failed" and parent.journal_cursor == paid.snapshot.cursor + 1
            assert attempt.settled_at is not None and hold.state == "consumed"
            assert ledger.prompt_tokens == 7 and ledger.completion_tokens == 3
            assert effect.disposition == "refused" and effect.provider_attempt_id == attempt.attempt_id
            return (parent.checkpoint_id, parent.journal_cursor, parent.failure_code, parent.result_sha256, attempt, hold, ledger, effect)

        async def join_stage(lease, stage):
            result = await original_join(lease, stage)
            if isinstance(result, SemanticAdvisorPaidInvalidTerminal):
                assert not returned
                assert isinstance(result.original_error, _MalformedLLMResponseError)
                assert result.original_error is result.paid.original_error
                assert result.paid.outcome is semantic_sql.SemanticAdvisorProviderOutcome.PAID_INVALID
                assert result.paid.response is not None
                assert decode_continuation_response(result.paid.response).message.content == private_text
                returned.append((result, asyncio.current_task()))
                committed.append(committed_state())
            return result

        def cold_read(*args, **kwargs):
            result = original_cold(*args, **kwargs)
            if fault == "cold_read" and returned and not injected and args[2] == operation_id:
                assert result[0] == returned[0][0].paid
                injected.append(secondary)
                raise secondary
            return result

        async def finish_watcher(watcher, observation):
            failures = await original_cleanup(watcher, observation)
            if fault == "watcher_cleanup" and returned and not injected and asyncio.current_task() is returned[0][1]:
                assert watcher.done() and not failures
                injected.append(secondary)
                raise secondary
            return failures

        def join_failure(driver, error, *additional):
            result = original_failure(driver, error, *additional)
            if driver.running.claim.operation_id == operation_id and _contains(result, secondary):
                assert len(returned) == 1
                assert _contains(result, returned[0][0].original_error)
                joined.append(result)
                failure_observed.set()
            return result

        committed = []
        monkeypatch.setattr(SessionOperationLease, "join_continuation_stage", join_stage)
        monkeypatch.setattr(driver_module, "_paid_result_data", cold_read)
        monkeypatch.setattr(worker, "_finish_operation_watcher", finish_watcher)
        monkeypatch.setattr(driver_module._SegmentDriver, "join_failure", join_failure)

        def malformed(_request):
            retained = effects(harness.app, operation_id)
            assert any(row.owner == "tool_dispatch" and row.disposition == "committed" for row in retained)
            assert any(row.effect_key.endswith(":p4") and row.disposition == "committed" for row in retained)
            assert sdk.roles == ["primary", "semantic"]
            return sdk_reply(private_text, model=ADVISOR_MODEL, cost=0.01)

        sdk = ScriptedSDK(
            [
                ("primary", lambda app: ("Inspecting the pipeline.", [tool_call(app, "mp1_predecessor", "get_pipeline_state", {})])),
                ("semantic", malformed),
            ],
            app_ref=[harness.app],
        )
        install_sdk(monkeypatch, sdk)
        artifact = tmp_path / f"paid-invalid-{fault}.json"
        with observe_supported_path(monkeypatch, operation_id=operation_id, session_id=harness.session_id, artifact=artifact) as observed:
            terminal = await submit_and_settle(
                harness, message_body("Inspect the pipeline and explain its current state.", operation_id=operation_id), rounds=720
            )
            # Receiving synchronization only; production clocks remain original.
            await asyncio.wait_for(failure_observed.wait(), timeout=10)
            observed.require_driver_stages()
            assert injected == [secondary] and joined
            assert committed_state() == committed[0]
            assert terminal.status == "failed" and terminal.error is not None
            assert terminal.error.failure_code == "operation_failed"
            assert private_text not in terminal.model_dump_json()
            with harness.app.state.session_engine.connect() as conn:
                attempts = conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id))).all()
                paid = returned[0][0].paid
                cold, _input, _root = original_cold(
                    conn,
                    str(harness.session_id),
                    operation_id,
                    paid.snapshot,
                    decode_continuation_snapshot(paid.snapshot.payload_json),
                    paid.effect_key,
                )
            assert cold == paid
            assert len(attempts) == 2 and all(attempt.settled_at is not None for attempt in attempts)
            assert sdk.roles == ["primary", "semantic"] and not sdk.steps
        assert private_text not in artifact.read_text(encoding="utf-8")
    assert sdk.roles == ["primary", "semantic"]
