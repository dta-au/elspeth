"""SDK-entry progress follows actual PRIMARY custody, not dispatch intent.

The PRIMARY fixture retains real SQL, guard, quota, cost, audit and progress
owners. Only the external SDK response and selected refusal seam are controlled.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import litellm
import pytest

from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionRefused,
    QuotaDisposition,
)
from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import provider_gateway as gateway_source
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.continuation_primary import PrimaryNoSdkRefusalReceipt
from elspeth.web.sessions.continuation_progress_owner import ContinuationProgressOwner
from tests.helpers.continuation_budgets import OWNER_LEASE_SECONDS
from tests.unit.web.sessions.test_continuation_prompt_producers import REQUEST
from tests.unit.web.sessions.test_continuation_runtime_provider_history import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_runtime_provider_history import prepared_runtime

operation_store = _operation_store


@asynccontextmanager
async def _actual_owner(store, tmp_path, monkeypatch, registry: ComposerProgressRegistry | None = None):
    arguments, runtime, sdk_calls = await prepared_runtime(store, tmp_path, monkeypatch)
    work = arguments["required_work"]
    running = work.running
    assert running is not None
    adoption = ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS, required_work=work.coordinator
    )
    lease = await adoption.join()
    record = arguments["authority"].get(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
    assert record is not None
    actual_registry = ComposerProgressRegistry() if registry is None else registry
    progress = ContinuationProgressOwner(
        registry=actual_registry,
        running=running,
        lease=lease,
        record=record,
        renew_interval_seconds=60.0,
    )
    try:
        await progress.start()
        yield arguments, runtime, sdk_calls, progress, actual_registry
    finally:
        await lease.close()


async def _primary(arguments, runtime, on_sdk_entry):
    with prompt_guard_user_scope(REQUEST):
        return await run_continuation_primary(
            service=arguments["service"],
            authority=arguments["authority"],
            selected=arguments["selected"],
            producers=runtime.materialization,
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            drained=runtime.drained,
            required_work=arguments["required_work"],
            provider_owner=arguments["provider_owner"],
            timeout=runtime.source.policy.actions.primary.total,
            on_sdk_entry=on_sdk_entry,
        )


async def _wait_for_entry_or_failure(entered: asyncio.Event, primary: asyncio.Task[object]) -> None:
    waiter = asyncio.create_task(entered.wait())
    try:
        done, _pending = await asyncio.wait({waiter, primary}, timeout=30.0, return_when=asyncio.FIRST_COMPLETED)
        if primary in done:
            await primary
            pytest.fail("PRIMARY completed without entering the parked SDK")
        assert waiter in done, "PRIMARY did not enter the SDK within 30 seconds"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_parked_primary_reports_only_its_actual_sdk_entry(operation_store, tmp_path, monkeypatch):
    async with _actual_owner(operation_store, tmp_path, monkeypatch) as (arguments, runtime, calls, progress, registry):
        entered, release = asyncio.Event(), asyncio.Event()
        actual_completion = litellm.acompletion
        observed_entries: list[str] = []

        async def parked(**kwargs):
            entered.set()
            await release.wait()
            return await actual_completion(**kwargs)

        def notify() -> None:
            observed_entries.append("sdk")
            progress.notify_sdk_entry()

        monkeypatch.setattr(litellm, "acompletion", parked)
        task = asyncio.create_task(_primary(arguments, runtime, notify))
        try:
            await _wait_for_entry_or_failure(entered, task)
            running = arguments["required_work"].running
            assert running is not None
            fence = running.session_operation_context.fence

            async def current_entry():
                while True:
                    snapshot = await registry.get_for_operation(
                        session_id=str(running.claim.session_id),
                        user_id=progress.record.actor_user_id,
                        operation_id=running.claim.operation_id,
                        session_operation_id=fence.operation_id,
                        session_operation_epoch=fence.operation_epoch,
                    )
                    if snapshot is not None and snapshot.phase == "calling_model":
                        return snapshot
                    if task.done():
                        await task
                    await asyncio.sleep(0.001)

            snapshot = await asyncio.wait_for(current_entry(), timeout=10.0)
            assert observed_entries == ["sdk"] and calls == []
            assert snapshot.request_id == str(progress.record.reserved_user_message_id)
            assert snapshot.request_token == progress.request_lease.request_token
            assert snapshot.inflight_requests == 1
        finally:
            release.set()
            finished, _pending = await asyncio.wait({task}, timeout=30.0)
            assert task in finished, "the original PRIMARY did not finish after SDK release"
            try:
                result = task.result()
            finally:
                await progress.close()
        assert result.outcome.audit_call is not None and len(calls) == 1
        assert progress.unregistered and (await registry.get_latest(str(progress.record.session_id))).inflight_requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("guard", "quota", "final_clock"))
async def test_pre_sdk_refusal_never_reports_model_entry(operation_store, tmp_path, monkeypatch, stage):
    async with _actual_owner(operation_store, tmp_path, monkeypatch) as (arguments, runtime, calls, progress, registry):
        entries: list[str] = []

        def notify() -> None:
            entries.append("sdk")
            progress.notify_sdk_entry()

        if stage == "guard":

            async def refuse_guard(**_kwargs):
                raise AuditIntegrityError("controlled prompt guard refusal before SDK")

            monkeypatch.setattr(gateway_source, "enforce_prompt_guard", refuse_guard)
        elif stage == "quota":
            refused = ChargeableAdmissionRefused(
                ChargeableAdmissionDecision(
                    refusal_reason=AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE,
                    evidence=AdmissionPolicyEvidence(
                        identity_policy_id="current-identity-policy",
                        quota_disposition=QuotaDisposition.ACCOUNTING_UNAVAILABLE,
                        secret_wiring_hash="a" * 64,
                    ),
                )
            )

            async def refuse_quota(_custody, *, model):
                assert model == "test/model"
                raise refused

            monkeypatch.setattr(ProviderCallCustody, "admit", refuse_quota)
        else:
            original_run = ContinuationActionClock.run

            async def expire_at_physical(clock, current_stage, operation):
                if current_stage == "physical":
                    # Exercise the gateway's final original-clock check after
                    # real guard, quota and cost dispatch have succeeded.
                    clock.deadline = asyncio.get_running_loop().time() - 0.001
                    return await operation()
                return await original_run(clock, current_stage, operation)

            monkeypatch.setattr(ContinuationActionClock, "run", expire_at_physical)

        try:
            receipt = await _primary(arguments, runtime, notify)
            assert type(receipt) is PrimaryNoSdkRefusalReceipt
            assert receipt.terminal.status == "failed" and receipt.effect_key
            assert entries == [] and calls == []
            latest = await registry.get_latest(str(progress.record.session_id))
            assert latest.phase != "calling_model"
        finally:
            await progress.close()
        assert progress.unregistered


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("event", "schedule"))
async def test_synchronous_entry_notification_defect_does_not_abort_paid_sdk(operation_store, tmp_path, monkeypatch, fault):
    async with _actual_owner(operation_store, tmp_path, monkeypatch) as (arguments, runtime, calls, progress, _registry):
        from elspeth.web.sessions import continuation_progress_owner as owner_source

        original = RuntimeError(f"controlled synchronous progress {fault} defect")
        rejected_coroutines = []

        def broken_event(_message):
            raise original

        if fault == "event":
            monkeypatch.setattr(owner_source, "model_call_progress_event", broken_event, raising=False)
        else:
            actual_create_task = asyncio.create_task

            def fail_entry_schedule(coro, *, name=None, context=None):
                if name == "continuation-progress-sdk-entry":
                    rejected_coroutines.append(coro)
                    raise original
                return actual_create_task(coro, name=name, context=context)

            monkeypatch.setattr(asyncio, "create_task", fail_entry_schedule)
        result = await _primary(arguments, runtime, progress.notify_sdk_entry)
        assert result.outcome.audit_call is not None and len(calls) == 1
        with pytest.raises(RuntimeError) as caught:
            await progress.close()
        assert caught.value is original and progress.unregistered
        if fault == "schedule":
            assert len(rejected_coroutines) == 1 and rejected_coroutines[0].cr_frame is None


@pytest.mark.asyncio
async def test_fast_primary_return_joins_queued_entry_before_retiring_token(operation_store, tmp_path, monkeypatch):
    registry = ComposerProgressRegistry()
    original_claim = registry.claim_request
    entered, release = asyncio.Event(), asyncio.Event()

    async def claim(**kwargs):
        publisher = await original_claim(**kwargs)

        async def held(event):
            if event.phase == "calling_model":
                entered.set()
                await release.wait()
            await publisher(event)

        return held

    monkeypatch.setattr(registry, "claim_request", claim)
    async with _actual_owner(operation_store, tmp_path, monkeypatch, registry) as (arguments, runtime, calls, progress, _registry):
        result = await _primary(arguments, runtime, progress.notify_sdk_entry)
        assert result.outcome.audit_call is not None and len(calls) == 1
        closing = asyncio.create_task(progress.close())
        try:
            await asyncio.wait_for(entered.wait(), timeout=15.0)
            assert not closing.done(), "close must join the queued SDK-entry write"
            assert (await registry.get_latest(str(progress.record.session_id))).inflight_requests == 1
        finally:
            release.set()
            await asyncio.wait_for(closing, timeout=30.0)
        latest = await registry.get_latest(str(progress.record.session_id))
        assert latest.phase == "calling_model" and latest.inflight_requests == 0 and progress.unregistered


@pytest.mark.asyncio
async def test_sdk_entry_publication_precedes_later_terminal_event(operation_store, tmp_path, monkeypatch):
    registry = ComposerProgressRegistry()
    original_claim = registry.claim_request
    entered, release = asyncio.Event(), asyncio.Event()
    ordered: list[str] = []

    async def claim(**kwargs):
        publisher = await original_claim(**kwargs)

        async def held(event):
            if event.phase == "calling_model":
                entered.set()
                await release.wait()
            await publisher(event)
            ordered.append(event.phase)

        return held

    monkeypatch.setattr(registry, "claim_request", claim)
    async with _actual_owner(operation_store, tmp_path, monkeypatch, registry) as (arguments, runtime, calls, progress, _registry):
        result = await _primary(arguments, runtime, progress.notify_sdk_entry)
        assert result.outcome.audit_call is not None and len(calls) == 1
        terminal = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=15.0)
            terminal = asyncio.create_task(progress.sink(ComposerProgressEvent(phase="complete", headline="Completed actual work")))
            await asyncio.sleep(0)
            assert not terminal.done(), "terminal progress must wait for its earlier SDK entry"
        finally:
            release.set()
            if terminal is not None:
                await asyncio.wait_for(terminal, timeout=30.0)
            await progress.close()
        assert ordered == ["calling_model", "complete"]
        latest = await registry.get_latest(str(progress.record.session_id))
        assert latest.phase == "complete" and latest.inflight_requests == 0 and progress.unregistered
