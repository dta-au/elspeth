"""Original PRIMARY SDK failures survive the actual driver and cold release."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import litellm
import pytest
from openai import OpenAIError
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.provider_sdk_terminal import require_sdk_terminal_failure
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.continuation_driver import ContinuationSegmentSettled, _SegmentDriver
from elspeth.web.sessions.continuation_primary_sdk_terminal import read_v4_primary_sdk_terminal_verified_on_connection
from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_budgets import OWNER_LEASE_SECONDS
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input
from tests.unit.web.sessions.test_continuation_tool_failure import _claimed_driver, _unstarted_driver

operation_store = operations.operation_store


@pytest.mark.parametrize("audit_companion", (False, True), ids=("sdk-failure", "sdk-and-owned-audit-failure"))
@pytest.mark.asyncio
async def test_actual_driver_cold_verifies_sdk_failure_without_retry_or_terminal_overwrite(
    operation_store, tmp_path, monkeypatch, audit_companion
):
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    runtime, initial_work, _requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    initial_work.coordinator.assert_completed()
    running = initial_work.running
    assert running is not None
    observed = authority.read_continuation_timing(running)
    drain = ComposerDrainSignal()
    sql_clock = ContinuationSQLDeadlineOwner(
        running,
        policy=policy,
        drain_signal=drain,
        drain_seconds=10.0,
        hard_expires_at=time.monotonic() + observed.hard_remaining_seconds,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            running.claim.operation_id,
            running.claim.attempt,
        ),
        sql_deadline_owner=sql_clock,
    )
    work = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN, running)
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS, required_work=coordinator
    ).join()
    provider = ProviderInvocationOwner(service=store[3], required_work=work)
    phases = ContinuationLivePhases(runtime.materialization.service, authority, work, provider, lease)
    driver = _unstarted_driver(store, phases, running, drain_signal=drain)
    key = (running.claim.session_id, running.claim.operation_id)
    sdk_calls = []
    sdk_failure = OpenAIError("private SDK failure must not become public evidence")
    audit_failure = AuditIntegrityError("owned audit constructor failure")

    async def completion(**kwargs):
        sdk_calls.append(kwargs["model"])
        raise sdk_failure

    def fail_audit(**_kwargs):
        raise audit_failure

    monkeypatch.setattr(litellm, "acompletion", completion)
    if audit_companion:
        monkeypatch.setattr(provider_gateway, "build_llm_call_record", fail_audit)
    releases = []
    failures = []
    actual_verify = ComposerAsyncWorker._verify_terminal_before_release
    actual_consume = _SegmentDriver.consume_sdk_terminal

    async def observe_verify(self, original_running, original_lease, terminal, settlement):
        await actual_verify(self, original_running, original_lease, terminal, settlement)
        if self is driver.worker:
            releases.append(terminal)

    async def observe_consume(self, result):
        if self is driver and type(result) is SdkTerminalFailure:
            require_sdk_terminal_failure(result)
            with pytest.raises(AuditIntegrityError):
                require_sdk_terminal_failure(replace(result))
            failures.append(result)
        return await actual_consume(self, result)

    monkeypatch.setattr(ComposerAsyncWorker, "_verify_terminal_before_release", observe_verify)
    monkeypatch.setattr(_SegmentDriver, "consume_sdk_terminal", observe_consume)
    try:
        if audit_companion:
            with pytest.raises(BaseExceptionGroup) as caught:
                async with _claimed_driver(driver):
                    assert type(driver.worker._local_cancels[key]) is asyncio.Event
                    await driver.run(None)
            assert len(failures) == len(releases) == 1
            assert caught.value is failures[0].original
            assert caught.value.exceptions == (sdk_failure, audit_failure)
            terminal = releases[0]
        else:
            async with _claimed_driver(driver):
                assert type(driver.worker._local_cancels[key]) is asyncio.Event
                outcome = await driver.run(None)
            assert type(outcome) is ContinuationSegmentSettled and outcome.terminal.status == "failed"
            assert releases == [outcome.terminal] and not failures
            terminal = outcome.terminal
        assert key not in driver.worker._local_cancels
        assert driver.progress_owner is not None and driver.progress_owner.unregistered
        assert not driver.progress_owner.contained_pending and driver.progress_owner.cleanup_failure is None
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
            attempts = conn.execute(select(physical).where(physical.c.session_id == sid)).all()
            ledger = conn.execute(select(usage).where(usage.c.session_id == sid)).all()
            retained_holds = conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation)).all()
            assert len(sdk_calls) == len(attempts) == len(ledger) == len(retained_holds) == 1
            cold = read_v4_primary_sdk_terminal_verified_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=retained_holds[0].effect_key
            )
        assert cold is not None and cold.hold_state == "consumed" and cold.receipt.retryable is False
        assert attempts[0].settled_at is not None and attempts[0].ledger_entry_id == ledger[0].entry_id
        assert retained_holds[0].state == "consumed" and parent.status == "failed"
        assert (terminal.checkpoint_id, terminal.journal_cursor) == (cold.terminal.checkpoint_id, cold.terminal.cursor)
        public = json.loads(parent.result_json)
        assert public["http_status"] == 502 and public["error_type"] == "provider_sdk_terminal"
        assert public["body"]["retryable"] is False and public["body"]["cost"]["status"] == "unknown"
        assert "private SDK failure" not in parent.result_json and "owned audit constructor failure" not in parent.result_json
        if audit_companion:
            assert driver._consumed_native
            errors = BaseExceptionGroup("actual driver failure observations", driver._consumed_native)
            assert errors.subgroup(lambda error: error is sdk_failure) is not None
            assert errors.subgroup(lambda error: error is audit_failure) is not None
        work.coordinator.assert_completed()
    finally:
        await lease.close(deadline=driver.worker.drain_deadline)
