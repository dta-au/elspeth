"""Actual live receipt propagation from explicit historical V3 paid custody.

The historical SQL fixture does not establish admission of a fresh paid V3
PRIMARY. From that retained cut onward the service, phases, lease stages,
driver and terminal SQL readers are production owners.
"""

import asyncio
from contextlib import asynccontextmanager

import litellm
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_live_phases as phases_module
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV3
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.continuation_driver import ContinuationSegmentSettled
from elspeth.web.sessions.continuation_tool_no_execution import ToolNoExecutionRefusal
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_budgets import OWNER_LEASE_SECONDS
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_live_phases import _phase_case
from tests.unit.web.sessions.test_continuation_tool_failure import _actual_failure_driver, _install_original_deadline
from tests.unit.web.sessions.test_continuation_v3_tool_historical_refusal import (
    _cold,
    _rows,
    historical_tool_cut,
)

operation_store = test_composer_operation_authority.operation_store


@asynccontextmanager
async def _historical_driver(operation_store, tmp_path, monkeypatch, *, historical_values=None):
    if historical_values is None:
        _install_original_deadline(operation_store, monkeypatch)
        values = await historical_tool_cut(operation_store, tmp_path, monkeypatch, initialized=True)
    else:
        values = historical_values
    store, policy, running, selected, _inventory, _batch, _accumulator, completion, arguments = values
    work = arguments["required_work"]
    adoption = ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS, required_work=work.coordinator
    )
    lease = await adoption.join()
    # This is a cold driver after historical paid accounting was inserted.
    # Reacquire the provider owner as _SegmentDriver.__init__ does, so restore
    # binds the complete durable cost snapshot rather than the pre-history one.
    provider = ProviderInvocationOwner(service=store[3], required_work=work)
    owner = ContinuationLivePhases(arguments["service"], store[2], work, provider, lease)
    driver = _actual_failure_driver(store, owner, running)
    # Keep the existing work/provider identities bound to the real SQL clock.
    # The driver starts cold and restores the historical source itself.
    driver.work, driver.provider = work, owner.provider_owner
    # _claim_once installs this exact operation's local Stop event before
    # scheduling _job. This fixture enters at the already-started segment.
    key = (running.claim.session_id, running.claim.operation_id)
    local_cancel = asyncio.Event()
    assert key not in driver.worker._local_cancels
    driver.worker._local_cancels[key] = local_cancel
    original = None
    try:
        # The fixture's paid response is historical DATA. Use the real phase
        # producer to retain the shared evidence/batch graph before testing a
        # cold driver at that actual unissued tool cut.
        initializing_provider = ProviderInvocationOwner(service=store[3], required_work=work)
        initializing = ContinuationLivePhases(arguments["service"], store[2], work, initializing_provider, lease)
        assert (
            await arguments["service"].advance_continuation_phase(
                owner=initializing,
                authority=store[2],
                selected=selected,
                work=work,
                provider_owner=initializing_provider,
            )
            is None
        )
        runtime = initializing.runtime
        assert runtime is not None and runtime.graph.batch is not None
        assert type(runtime.source.policy) is ContinuationAdmissionPolicyV3
        assert runtime.source.policy == policy
        assert runtime.graph.batch.batch.phase == "call_ready"
        assert runtime.source.selected.stack.frames[-1].phase == "tool_ready"
        values = (
            store,
            policy,
            running,
            runtime.source.selected,
            runtime.materialization.inventory,
            runtime.graph.batch.batch,
            runtime.graph.batch.accumulator,
            completion,
            arguments,
        )
        actual_current = ContinuationLivePhases.current

        async def require_actual_v3_current(self, selected):
            restored, inventory = await actual_current(self, selected)
            if self is owner:
                assert type(restored.source.policy) is ContinuationAdmissionPolicyV3
                assert restored.source.policy == policy
                assert restored.source.selected.checkpoint_id == runtime.source.selected.checkpoint_id
            return restored, inventory

        monkeypatch.setattr(ContinuationLivePhases, "current", require_actual_v3_current)
        yield driver, values
    except BaseException as error:
        original = error
        raise
    finally:
        cleanup = []
        try:
            await driver.close_progress()
        except BaseException as error:
            cleanup.append(error)
        try:
            await lease.close(deadline=driver.worker.drain_deadline)
        except BaseException as error:
            cleanup.append(error)
        assert driver.worker._local_cancels.pop(key) is local_cancel
        if cleanup:
            raise BaseExceptionGroup("Historical driver cleanup", [*cleanup, *((original,) if original is not None else ())])


def _forbid_dispatch(monkeypatch):
    entered = []

    def tripwire(site):
        async def forbidden(*args, **kwargs):
            entered.append(site)
            raise AssertionError(f"V3 later-tool refusal entered dispatch producer: {site}")

        return forbidden

    monkeypatch.setattr(phases_module, "_prepare_continuation_call_live", tripwire("_prepare_continuation_call_live"))
    monkeypatch.setattr(phases_module, "_execute_continuation_call_live", tripwire("_execute_continuation_call_live"))
    monkeypatch.setattr(ContinuationLivePhases, "_advance_attachment", tripwire("_advance_attachment"))
    monkeypatch.setattr(ContinuationLivePhases, "_retain", tripwire("_retain"))
    monkeypatch.setattr(litellm, "acompletion", tripwire("litellm.acompletion"))
    return entered


def _read_refusal(store, running, inventory, receipt):
    with store[0].connect() as conn:
        return _cold(conn, store, running, inventory, receipt)


def _leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return tuple(leaf for child in error.exceptions for leaf in _leaves(child))
    return (error,)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ("ordinary", "cancel_after_commit", "drain_after_commit"))
async def test_actual_v3_tool_refusal_reaches_driver_terminal_without_dispatch(operation_store, tmp_path, monkeypatch, delivery):
    async with _historical_driver(operation_store, tmp_path, monkeypatch) as (driver, values):
        store, _policy, running, selected, inventory, *_ = values
        before = _rows(store[0], str(store[4]), running.claim.operation_id)
        entered = _forbid_dispatch(monkeypatch)
        observed = []
        releases = []
        actual = ComposerServiceImpl.advance_continuation_phase
        verify = ComposerAsyncWorker._verify_terminal_before_release

        async def verify_terminal(self, *args, **kwargs):
            result = await verify(self, *args, **kwargs)
            releases.append(args[2])
            return result

        async def deliver(self, **kwargs):
            result = await actual(self, **kwargs)
            assert type(result) is ToolNoExecutionRefusal
            # SQL commit and both phase/inner-lease returns have happened.
            assert _read_refusal(store, running, inventory, result) == result
            observed.append(result)
            if delivery == "cancel_after_commit":
                raise asyncio.CancelledError("after original refusal commit, before driver delivery")
            if delivery == "drain_after_commit":
                driver.worker.begin_drain()
            return result

        monkeypatch.setattr(ComposerServiceImpl, "advance_continuation_phase", deliver)
        monkeypatch.setattr(ComposerAsyncWorker, "_verify_terminal_before_release", verify_terminal)
        outcome = await driver.run(None)
        assert type(outcome) is ContinuationSegmentSettled
        assert observed, tuple((type(leaf).__name__, str(leaf)) for error in driver._consumed_native for leaf in _leaves(error))
        assert outcome.terminal.status == "failed"
        assert outcome.terminal.journal_cursor == selected.stack.cursor + 2
        assert len(observed) == 1 and not entered
        assert releases == [outcome.terminal]
        receipt = observed[0]
        assert receipt.source_checkpoint_id == selected.checkpoint_id
        assert receipt.source_cursor == selected.stack.cursor
        after = _rows(store[0], str(store[4]), running.claim.operation_id)
        # The original provider attempt, ledger and audit message stay intact.
        for table in (physical, usage, messages):
            assert after[table.name] == before[table.name]
        assert _read_refusal(store, running, inventory, receipt) == receipt
        assert _read_refusal(store, running, inventory, receipt) == receipt
        assert _rows(store[0], str(store[4]), running.claim.operation_id) == after
        assert driver.phases.primary is not None
        assert driver.phases.business_stage is not None
        assert driver.phases.business_stage.task is not None
        assert driver.phases.business_stage.task.done()
        assert driver.phases.business_stage.task.result() == receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_member", ("source_cursor", "creator_generation", "operation_epoch"))
async def test_driver_rejects_returned_receipt_substitution_after_real_commit(operation_store, tmp_path, monkeypatch, changed_member):
    # Fixture failures must retain their own traceback, outside the expected
    # driver-rejection boundary.
    _install_original_deadline(operation_store, monkeypatch)
    historical_values = await historical_tool_cut(operation_store, tmp_path, monkeypatch, initialized=True)
    observed, releases = [], []
    actual = ComposerServiceImpl.advance_continuation_phase
    verify = ComposerAsyncWorker._verify_terminal_before_release

    async def substitute(self, **kwargs):
        receipt = await actual(self, **kwargs)
        assert type(receipt) is ToolNoExecutionRefusal
        changed = {
            "source_cursor": receipt.source_cursor + 1,
            "creator_generation": receipt.creator_generation + 1,
            "operation_epoch": receipt.operation_epoch + 1,
        }
        observed.append(receipt)
        return receipt.model_copy(update={changed_member: changed[changed_member]})

    async def verify_terminal(self, *args, **kwargs):
        releases.append(args[2])
        return await verify(self, *args, **kwargs)

    with pytest.raises(BaseException) as caught:
        async with _historical_driver(operation_store, tmp_path, monkeypatch, historical_values=historical_values) as (driver, values):
            store, _policy, running, _selected, inventory, *_ = values
            entered = _forbid_dispatch(monkeypatch)
            monkeypatch.setattr(ComposerServiceImpl, "advance_continuation_phase", substitute)
            monkeypatch.setattr(ComposerAsyncWorker, "_verify_terminal_before_release", verify_terminal)
            await driver.run(None)
            pytest.fail(
                "Driver returned instead of rejecting the substituted receipt; consumed failures: "
                + repr(tuple((type(leaf).__name__, str(leaf)) for error in driver._consumed_native for leaf in _leaves(error)))
            )
    leaves = _leaves(caught.value)
    assert any(
        isinstance(error, AuditIntegrityError) and str(error) == "V3 tool refusal lost its original committed observation and inventory"
        for error in leaves
    ), tuple((type(error).__name__, str(error)) for error in leaves)
    assert len(observed) == 1 and not releases and not entered
    assert _read_refusal(store, running, inventory, observed[0]) == observed[0]


@pytest.mark.asyncio
async def test_driver_recovers_refusal_cancelled_after_sql_registration_before_phase_delivery(operation_store, tmp_path, monkeypatch):
    async with _historical_driver(operation_store, tmp_path, monkeypatch) as (driver, values):
        store, _policy, running, selected, inventory, *_ = values
        before = _rows(store[0], str(store[4]), running.claim.operation_id)
        entered = _forbid_dispatch(monkeypatch)
        registered = phases_module._registered
        verify = ComposerAsyncWorker._verify_terminal_before_release
        receipts, releases = [], []

        async def cancel_delivery(*args, **kwargs):
            result = await registered(*args, **kwargs)
            if type(result) is ToolNoExecutionRefusal:
                observed = driver.phases.tool_refusal
                assert type(observed) is phases_module.CommittedToolNoExecutionRefusal
                assert observed.receipt is result
                assert observed.inventory.running is running
                assert observed.inventory.names == inventory.names
                assert _read_refusal(store, running, inventory, result) == result
                receipts.append(result)
                raise asyncio.CancelledError("after registered SQL commit, before phase delivery")
            return result

        async def verify_terminal(self, *args, **kwargs):
            result = await verify(self, *args, **kwargs)
            releases.append(args[2])
            return result

        monkeypatch.setattr(phases_module, "_registered", cancel_delivery)
        monkeypatch.setattr(ComposerAsyncWorker, "_verify_terminal_before_release", verify_terminal)
        outcome = await driver.run(None)
        assert type(outcome) is ContinuationSegmentSettled
        assert outcome.terminal.status == "failed"
        assert outcome.terminal.journal_cursor == selected.stack.cursor + 2
        assert len(receipts) == 1 and releases == [outcome.terminal] and not entered
        assert driver.phases.tool_refusal is not None
        assert driver.phases.tool_refusal.receipt is receipts[0]
        assert _read_refusal(store, running, inventory, receipts[0]) == receipts[0]
        after = _rows(store[0], str(store[4]), running.claim.operation_id)
        for table in (physical, usage, messages):
            assert after[table.name] == before[table.name]
        assert driver.phases.business_stage is not None
        assert driver.phases.business_stage.task is not None
        assert driver.phases.business_stage.task.cancelled()


@pytest.mark.asyncio
async def test_v2_same_live_service_seam_prepares_and_executes_original_tool(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk_calls):
        prepare = phases_module._prepare_continuation_call_live
        execute = phases_module._execute_continuation_call_live
        prepared, executed = [], []

        async def observe_preparation(**kwargs):
            prepared.append(kwargs["call"])
            return await prepare(**kwargs)

        async def observe_execution(**kwargs):
            executed.append(kwargs["call"])
            return await execute(**kwargs)

        monkeypatch.setattr(phases_module, "_prepare_continuation_call_live", observe_preparation)
        monkeypatch.setattr(phases_module, "_execute_continuation_call_live", observe_execution)
        result = await owner.service.advance_continuation_phase(
            owner=owner,
            authority=owner.authority,
            selected=owner.runtime.source.selected,
            work=owner.work,
            provider_owner=owner.provider_owner,
        )
        assert result is None
        assert len(prepared) == len(executed) == 1
        assert prepared[0] is executed[0]
        assert len(sdk_calls) == 1
