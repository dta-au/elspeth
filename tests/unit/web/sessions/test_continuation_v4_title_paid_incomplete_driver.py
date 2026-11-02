"""An actual V4 TITLE paid-incomplete result reaches the worker's public terminal."""

from __future__ import annotations

import asyncio
import json
import time

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_title_provider import TitlePaidIncompleteResult
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions import continuation_driver as driver_module
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.continuation_driver import ContinuationSegmentSettled
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.continuation_title_paid_incomplete_terminal import read_v4_title_paid_incomplete_terminal_on_connection
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_budgets import OWNER_LEASE_SECONDS
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions import test_continuation_title_provider as title_cases
from tests.unit.web.sessions.test_continuation_tool_failure import _actual_failure_driver

operation_store = operations.operation_store


@pytest.mark.asyncio
async def test_v4_title_paid_incomplete_reaches_one_cold_public_507_without_retry(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, completed, service, _prior, _base = await title_cases.bootstrap_case(store)
    published = await title_cases.publish(store, running, completed, service)
    pre_title = decode_continuation_snapshot(published.selected.payload_json).frames[0]
    assert pre_title.ingress_phase == "title_pending" and pre_title.provider_effect_key is None
    with store[0].begin() as conn:
        title_cases.grant_test_pipeline_user(conn, identity_id="alice")
        title_cases.seed_token_policies(conn, identity_id="alice")
    settings, _guards = title_cases._required_guard_settings(tmp_path, monkeypatch)
    service._settings = settings
    service._provider_gateway = ProviderGateway(
        model=settings.composer_model, settings=settings, endpoint_base_url=None, endpoint_api_key=None
    )
    timing = authority.read_continuation_timing(running)
    drain_signal = ComposerDrainSignal()
    deadline = ContinuationSQLDeadlineOwner(
        running,
        policy=policy,
        drain_signal=drain_signal,
        drain_seconds=10.0,
        hard_expires_at=time.monotonic() + timing.hard_remaining_seconds,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            running.claim.operation_id,
            running.claim.attempt,
        ),
        sql_deadline_owner=deadline,
    )
    work = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN, running)
    adoption = ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS, required_work=work.coordinator
    )
    lease = await adoption.join()
    provider = ProviderInvocationOwner(service=store[3], required_work=work)
    owner = ContinuationLivePhases(service, authority, work, provider, lease)
    driver = _actual_failure_driver(store, owner, running, drain_signal=drain_signal)
    driver.work, driver.provider = work, provider
    key = (running.claim.session_id, running.claim.operation_id)
    local_cancel = asyncio.Event()
    assert key not in driver.worker._local_cancels
    driver.worker._local_cancels[key] = local_cancel
    sdk_calls: list[str] = []
    observed: list[TitlePaidIncompleteResult] = []
    prepared_titles = []
    advances: list[tuple[int, str, str]] = []

    async def completion(**kwargs):
        sdk_calls.append(kwargs["model"])
        return ModelResponse(
            id="returned-incomplete-title-driver",
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

    actual_run_title = driver_module.run_prepared_title_provider
    actual_advance = driver_module._SegmentDriver.advance

    async def observe_advance(self, stored, selected):
        frame = selected.stack.frames[-1]
        phase = frame.ingress_phase if frame.frame_kind == "initialize" else frame.phase
        result = await actual_advance(self, stored, selected)
        advances.append((stored.cursor, phase, type(result).__name__))
        return result

    async def observe_title(*, prepared, invocation_owner):
        prepared_titles.append(prepared)
        result = await actual_run_title(prepared=prepared, invocation_owner=invocation_owner)
        assert type(result) is TitlePaidIncompleteResult
        observed.append(result)
        return result

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(driver_module, "run_prepared_title_provider", observe_title)
    monkeypatch.setattr(driver_module._SegmentDriver, "advance", observe_advance)
    try:
        outcome = await driver.run(None)
        assert type(outcome) is ContinuationSegmentSettled and outcome.terminal.status == "failed"
        with store[0].connect() as conn:
            parent = conn.execute(
                select(jobs).where(jobs.c.session_id == str(running.claim.session_id), jobs.c.operation_id == running.claim.operation_id)
            ).one()
            hold = conn.execute(
                select(holds).where(holds.c.session_id == str(running.claim.session_id), holds.c.operation_id == running.claim.operation_id)
            ).one_or_none()
            persisted_effects = conn.execute(
                select(effects).where(
                    effects.c.session_id == str(running.claim.session_id), effects.c.operation_id == running.claim.operation_id
                )
            ).all()
            attempts = conn.execute(select(physical).where(physical.c.session_id == str(running.claim.session_id))).all()
            ledger = conn.execute(select(usage).where(usage.c.session_id == str(running.claim.session_id))).all()
        assert len(sdk_calls) == 1 and len(observed) == 1, (
            advances,
            parent.status,
            parent.result_json,
            tuple((type(error).__name__, str(error).splitlines()[0]) for error in driver._consumed_native),
            len(attempts),
            len(ledger),
            tuple((effect.effect_key, effect.disposition, effect.provider_attempt_id) for effect in persisted_effects),
        )
        assert len(prepared_titles) == 1
        with store[0].connect() as conn:
            cold = read_v4_title_paid_incomplete_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, names=prepared_titles[0].names
            )
        assert cold == observed[0].verified and cold is not None and cold.hold_state == "consumed"
        assert parent.status == "failed" and parent.result_json is not None
        public = json.loads(parent.result_json)
        assert public["http_status"] == 507 and public["error_type"] == "paid_result_incomplete"
        assert public["body"]["retryable"] is False and public["body"]["evidence_status"] == "incomplete"
        assert (outcome.terminal.checkpoint_id, outcome.terminal.journal_cursor) == (
            cold.terminal.checkpoint_id,
            cold.terminal.cursor,
        )
        assert hold is not None and hold.state == "consumed" and len(attempts) == len(ledger) == 1
        assert attempts[0].ledger_entry_id == ledger[0].entry_id
        assert cold.receipt.retryable is False and cold.receipt.provider_returned is True
        work.coordinator.assert_completed()
    finally:
        await driver.close_progress()
        await lease.close(deadline=driver.worker.drain_deadline)
        assert driver.worker._local_cancels.pop(key) is local_cancel
