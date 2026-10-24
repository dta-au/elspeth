"""Real TITLE provider/retention/current-accounting cuts; SDK doubles only."""

import asyncio
import json
from dataclasses import replace
from threading import Event
from time import sleep

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import detach_prompt_guard_messages
from elspeth.web.composer import continuation_title_provider as driver
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_title_values import decode_title_provider_result
from elspeth.web.composer.provider_audit_source import audit_drafts_json
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.provider_cost import CommonProviderCostBudget, CommonProviderCostSnapshot
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions import continuation_title_provider as sql_owner
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost, ComposerOperationRunning
from elspeth.web.sessions.continuation import ContinuationCASConflict
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.continuation_inventory import inventory_work
from tests.helpers.fenced_session import seed_token_policies
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted, _required_guard_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions.test_continuation_ingress import recovery_rows
from tests.unit.web.sessions.test_continuation_initialization import bootstrap_case, publish

operation_store = operations.operation_store


def raw_reply(model, content="Original Café Session Title"):
    return ModelResponse(
        id="actual-title-response",
        model=model,
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, "reasoning_content": "Keep the original café topic."},
            }
        ],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    )


async def title_case(store, tmp_path, monkeypatch):
    running, completed, service, _prior, _base = await bootstrap_case(store)
    published = await publish(store, running, completed, service)
    with store[0].begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
        seed_token_policies(conn, identity_id="alice")
    settings, guard_requests = _required_guard_settings(tmp_path, monkeypatch)
    service._settings = settings
    service._provider_gateway = ProviderGateway(
        model=settings.composer_model, settings=settings, endpoint_base_url=None, endpoint_api_key=None
    )
    work = inventory_work(running)
    prepared = await driver.prepare_current_title_provider(
        service=service, authority=store[2], selected=published.selected, required_work=work
    )
    return running, service, work, prepared, guard_requests


def sql_rows(store):
    with store[0].connect() as conn:
        return (
            tuple(conn.execute(select(physical)).all()),
            tuple(conn.execute(select(usage)).all()),
            tuple(conn.execute(select(messages).where(messages.c.session_id == str(store[4])).order_by(messages.c.sequence_no)).all()),
        )


def observer(monkeypatch, *, build_inputs=None):
    original = _auto_title.build_llm_call_record
    calls = []

    def build(**kwargs):
        if build_inputs is not None:
            build_inputs.append(kwargs)
        call = original(**kwargs)
        calls.append(call)
        return call

    monkeypatch.setattr(_auto_title, "build_llm_call_record", build)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ("Original Café Session Title", "", None, "Rejected/Title Candidate"))
async def test_actual_title_success_retains_original_full_response_and_single_accounting_without_title_write(
    operation_store, tmp_path, monkeypatch, content
):
    running, service, work, prepared, guard_requests = await title_case(operation_store, tmp_path, monkeypatch)
    build_inputs = []
    calls = observer(monkeypatch, build_inputs=build_inputs)
    contexts = []
    original_capture = _auto_title.capture_provider_context

    def capture_actual(**kwargs):
        context = original_capture(**kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(_auto_title, "capture_provider_context", capture_actual)
    sdk = []
    owner = ProviderInvocationOwner(service=operation_store[3], required_work=work)
    root = decode_continuation_snapshot(prepared.selected.payload_json)
    budget = CommonProviderCostBudget(
        CommonProviderCostSnapshot(str(operation_store[4]), root.logical.original_user_message_id, 2, "not_applicable", None)
    )
    owner.bind_cost_budget(budget)
    cost_before = budget.snapshot()
    with operation_store[0].connect() as conn:
        title_before = conn.execute(select(sessions.c.title).where(sessions.c.id == str(operation_store[4]))).scalar_one()

    async def completion(**kwargs):
        _assert_required_guard_admitted(
            operation_store[0],
            running.session_operation_context,
            guard_requests,
            role="composer",
            user_prompt="Preserve café and original context",
        )
        assert kwargs["messages"] == detach_prompt_guard_messages(prepared.request.audit_messages)[0]
        assert len(contexts) == 1 and not calls
        expected_wire = json.loads(sql_owner._descriptor(prepared.input).wire_json)
        assert contexts[0] == capture_provider_context(messages=kwargs["messages"], tools=None)
        assert contexts[0].disposition == "retained"
        assert contexts[0].canonical is not None
        captured_wire = json.loads(contexts[0].canonical)
        assert captured_wire["schema_version"] == "composer.provider-context.v1"
        assert captured_wire["capture_boundary"] == "prepared_litellm_messages_and_tools"
        assert captured_wire["messages"] == expected_wire["messages"] == kwargs["messages"]
        assert captured_wire["tools"] is None and expected_wire["tools_present"] is False
        assert kwargs["timeout"] == 5.0 and kwargs["max_tokens"] == 40
        assert kwargs["num_retries"] == kwargs["max_retries"] == 0 and "tools" not in kwargs
        rows, ledger, _messages = sql_rows(operation_store)
        assert len(rows) == 1 and not ledger and rows[0].settled_at is None
        sdk.append(kwargs)
        return raw_reply(kwargs["model"], content)

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        result = await driver.run_prepared_title_provider(prepared=prepared, invocation_owner=owner)
        assert result is not None and len(sdk) == len(calls) == 1
        assert result.audit_call is calls[0]
        assert len(build_inputs) == 1 and build_inputs[0]["private_request"] is contexts[0]
        assert calls[0].private_request is contexts[0]
        current = await driver.read_current_title_result(service=service, running=running, required_work=work)
        assert current.settled and current.settlement.result == result.result
        decoded = decode_title_provider_result(result.result, env_ref_names=current.settlement.inventory.names)
        assert decoded.completion.message.content == content
        assert decoded.completion.provider_metadata.reasoning_content == "Keep the original café topic."
        assert decoded.audit_call == calls[0]
        assert decoded.audit_call.private_request == contexts[0]
        assert audit_drafts_json(decoded.audit_drafts) == audit_drafts_json(result.audit_drafts)
        assert decoded.source.operation_id == running.session_operation_context.fence.operation_id
        rows, ledger, _messages = sql_rows(operation_store)
        assert len(rows) == len(ledger) == 1 and rows[0].ledger_entry_id == ledger[0].entry_id
        assert ledger[0].source == "auto_title" and ledger[0].prompt_tokens == 7 and ledger[0].completion_tokens == 3
        after = decode_continuation_snapshot(current.selected.payload_json)
        assert after.frames[0].ingress_phase == "title_pending" and not after.pending_settlement_ids
        assert after.mutation_ceiling == root.mutation_ceiling and after.logical == root.logical
        assert budget.snapshot() is cost_before
        with operation_store[0].connect() as conn:
            assert conn.execute(select(sessions.c.title).where(sessions.c.id == str(operation_store[4]))).scalar_one() == title_before
        before = recovery_rows(operation_store[0])
        assert (await driver.settle_current_title_result(service=service, running=running, required_work=work)).settled
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_retained_title_before_financial_settlement_reclaims_original_source_without_second_sdk(
    operation_store, tmp_path, monkeypatch
):
    running, service, work, prepared, guard_requests = await title_case(operation_store, tmp_path, monkeypatch)
    calls = observer(monkeypatch)
    reached, resume = asyncio.Event(), asyncio.Event()
    original_finish = type(operation_store[3]).finish_provider_attempt
    sdk = []

    async def pause_before_original(self, **kwargs):
        if kwargs["session_operation_context"] is running.session_operation_context:
            reached.set()
            await resume.wait()
        return await original_finish(self, **kwargs)

    async def completion(**kwargs):
        _assert_required_guard_admitted(
            operation_store[0],
            running.session_operation_context,
            guard_requests,
            role="composer",
            user_prompt="Preserve café and original context",
        )
        sdk.append(kwargs)
        return raw_reply(kwargs["model"])

    monkeypatch.setattr(type(operation_store[3]), "finish_provider_attempt", pause_before_original)
    monkeypatch.setattr(litellm, "acompletion", completion)
    task = asyncio.create_task(
        driver.run_prepared_title_provider(
            prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
        )
    )
    current_running = None
    released = False
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        retained = await driver.read_current_title_result(service=service, running=running, required_work=work)
        assert not retained.settled and retained.settlement.audit_call == calls[0]
        assert len(sdk) == 1 and not sql_rows(operation_store)[1]
        source = decode_title_provider_result(retained.settlement.result, env_ref_names=retained.settlement.inventory.names).source
        operation_store[1].release(running.session_operation_context)
        released = True
        current = new_owners(operation_store)
        engine, repository, authority, _sessions, sid = current
        assert repository.recover_composer_segment(
            session_id=sid,
            operation_id=running.claim.operation_id,
            expected_generation=running.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        (claim,) = authority.claim_segment(limit=1)
        context = repository.start_composer_segment(
            claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
        )
        current_running = ComposerOperationRunning(claim=claim, session_operation_context=context)
        fresh_work = inventory_work(current_running)
        await actual_title_update(current, current_running, fresh_work, "Renamed While Accounting Owed")
        # Reuse the same ordinary configured Composer owner against the same store;
        # no retained runtime context is relabeled into fresh authority.
        before = recovery_rows(engine)
        observed = await driver.read_current_title_result(service=service, running=current_running, required_work=fresh_work)
        assert recovery_rows(engine) == before and not observed.settled
        assert observed.settlement.audit_call == calls[0]
        finished = await driver.settle_current_title_result(service=service, running=current_running, required_work=fresh_work)
        assert finished.settled and len(sdk) == 1
        decoded = decode_title_provider_result(finished.settlement.result, env_ref_names=finished.settlement.inventory.names)
        assert decoded.source == source and decoded.source.operation_id != context.fence.operation_id
        assert decoded.audit_call.call_id == calls[0].call_id
        rows, ledger, _messages = sql_rows(current)
        assert len(rows) == len(ledger) == 1
        assert rows[0].operation_id == source.operation_id and rows[0].operation_epoch == source.operation_epoch
        with engine.connect() as conn:
            assert conn.execute(select(sessions.c.title).where(sessions.c.id == str(sid))).scalar_one() == "Renamed While Accounting Owed"
        untouched = recovery_rows(engine)
        assert (await driver.settle_current_title_result(service=service, running=current_running, required_work=fresh_work)).settled
        assert recovery_rows(engine) == untouched
        fresh_work.coordinator.assert_completed()
    finally:
        resume.set()
        if released:
            with pytest.raises((SessionOperationFenceLost, ComposerOperationFenceLost, AuditIntegrityError, BaseExceptionGroup)):
                await task
        else:
            await task
            operation_store[1].release(running.session_operation_context)
        work.coordinator.assert_completed()
        if current_running is not None:
            current[1].release(current_running.session_operation_context)


@pytest.mark.asyncio
async def test_title_original_retention_sql_error_rolls_back_chunk_event_root_and_still_settles_actual_call(
    operation_store, tmp_path, monkeypatch
):
    running, service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    marker = RuntimeError("original TITLE result commit failure")
    original_append = sql_owner._append
    before_retention = []

    def fail_received(conn, running, selected, following, descriptor, stage):
        if stage == "received":
            raise marker
        return original_append(conn, running, selected, following, descriptor, stage)

    async def completion(**kwargs):
        before_retention.append(operation_store[2].load_continuation_snapshot(running))
        return raw_reply(kwargs["model"])

    monkeypatch.setattr(sql_owner, "_append", fail_received)
    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        with pytest.raises(BaseException) as caught:
            await driver.run_prepared_title_provider(
                prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
            )

        def leaves(error):
            if isinstance(error, BaseExceptionGroup):
                return tuple(leaf for child in error.exceptions for leaf in leaves(child))
            return (error,)

        assert any(error is marker or error.__cause__ is marker for error in leaves(caught.value))
        selected = operation_store[2].load_continuation_snapshot(running)
        assert selected == before_retention[0]
        assert decode_continuation_snapshot(selected.payload_json).frames[0].title_response is None
        with operation_store[0].connect() as conn:
            assert not conn.execute(select(chunks).where(chunks.c.codec == "composer.title-provider-result.v1")).all()
            effect = conn.execute(select(effects).where(effects.c.effect_key == "provider:3:0")).one()
            assert effect.disposition == "dispatched" and effect.receipt_key is None and effect.result_chunk_key is None
        rows, ledger, _messages = sql_rows(operation_store)
        assert len(rows) == len(ledger) == 1 and rows[0].settled_at is not None
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="unresolved"):
            await driver.read_current_title_result(service=service, running=running, required_work=work)
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("source", "audit", "cohort", "context"))
async def test_title_current_source_rejects_changed_complete_retained_cohort_without_any_writes(
    operation_store, tmp_path, monkeypatch, mutation
):
    running, service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    monkeypatch.setattr(litellm, "acompletion", lambda **kwargs: asyncio.sleep(0, result=raw_reply(kwargs["model"])))
    try:
        await driver.run_prepared_title_provider(
            prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
        )
        good = await driver.read_current_title_result(service=service, running=running, required_work=work)
        assert good.settled
        bad = good.settlement
        if mutation == "audit":
            bad = replace(bad, audit_call=replace(bad.audit_call, messages_hash="f" * 64))
        elif mutation == "cohort":
            bad = replace(bad, audit_drafts=bad.audit_drafts[:-1])
        elif mutation == "context":
            bad = replace(bad, selected=prepared.selected)
        before = recovery_rows(operation_store[0])
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            if mutation == "source":
                decoded = decode_title_provider_result(bad.result, env_ref_names=bad.inventory.names)
                admitted = sql_owner.admit_title_audit_source_on_connection(
                    conn, running.session_operation_context, bad, names=bad.inventory.names
                )
                admitted.require(conn, running.session_operation_context, bad.audit_drafts)
                admitted.source = replace(decoded.source, operation_epoch=decoded.source.operation_epoch + 1)
                with pytest.raises(AuditIntegrityError, match="SQL-issued"):
                    admitted.require(conn, running.session_operation_context, bad.audit_drafts)
            else:
                with pytest.raises((AuditIntegrityError, ContinuationCASConflict)):
                    sql_owner.admit_title_audit_source_on_connection(
                        conn, running.session_operation_context, bad, names=bad.inventory.names
                    )
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_original_warm_title_interface_still_updates_once_and_returns_none(operation_store, tmp_path, monkeypatch):
    running, completed, _service, _prior, _base = await bootstrap_case(operation_store)
    settings, requests = _required_guard_settings(tmp_path, monkeypatch)
    gateway = ProviderGateway(model=settings.composer_model, settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    with operation_store[0].begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
        seed_token_policies(conn, identity_id="alice")
    work = inventory_work(running)
    custody = ProviderInvocationOwner(service=operation_store[3], required_work=work).mint(ProviderInvocationFamily.TITLE)
    build_inputs = []
    calls = observer(monkeypatch, build_inputs=build_inputs)
    sdk = []

    async def completion(**kwargs):
        _assert_required_guard_admitted(
            operation_store[0], running.session_operation_context, requests, role="composer", user_prompt=completed.message.content
        )
        sdk.append(kwargs)
        return raw_reply(kwargs["model"])

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        assert (
            await _auto_title.maybe_auto_title_session(
                service=operation_store[3],
                session_id=operation_store[4],
                user_message=completed.message.content,
                model=settings.composer_model,
                temperature=settings.composer_temperature,
                seed=settings.composer_seed,
                session_operation_context=running.session_operation_context,
                provider_custody=custody,
                prompt_guard_mount=gateway.prompt_guard_mount,
                prompt_guard_mode=settings.composer_prompt_guard_mode,
            )
            is None
        )
        assert len(sdk) == 1
        assert len(calls) == len(build_inputs) == 1
        assert "private_request" not in build_inputs[0] and calls[0].private_request is None
        with operation_store[0].connect() as conn:
            assert (
                conn.execute(select(sessions.c.title).where(sessions.c.id == str(operation_store[4]))).scalar_one()
                == "Original Café Session Title"
            )
            assert not conn.execute(select(effects)).all()
        assert len(sql_rows(operation_store)[1]) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_accounting_failure_rolls_back_complete_audit_ledger_and_root_then_replays_without_sdk(
    operation_store, tmp_path, monkeypatch
):
    running, service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    original_append = sql_owner._append
    original_finish = type(operation_store[3]).finish_provider_attempt
    marker = RuntimeError("original TITLE accounting root failure")
    sdk = []
    accounting_baselines = []

    def fail_settled(conn, running, selected, following, descriptor, stage):
        if stage == "settled":
            raise marker
        return original_append(conn, running, selected, following, descriptor, stage)

    async def completion(**kwargs):
        sdk.append(kwargs)
        return raw_reply(kwargs["model"])

    async def observe_accounting(self, **kwargs):
        assert self is operation_store[3]
        # The real guard, attempt and retained-response commits precede this
        # accounting transaction. Preserve all of them in the unfiltered oracle.
        accounting_baselines.append(recovery_rows(operation_store[0]))
        return await original_finish(self, **kwargs)

    monkeypatch.setattr(sql_owner, "_append", fail_settled)
    monkeypatch.setattr(type(operation_store[3]), "finish_provider_attempt", observe_accounting)
    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        with pytest.raises(BaseException) as caught:
            await driver.run_prepared_title_provider(
                prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
            )
        assert caught.value is marker or caught.value.__cause__ is marker
        assert len(accounting_baselines) == 1
        assert recovery_rows(operation_store[0]) == accounting_baselines[0]
        rows, ledger, _saved = sql_rows(operation_store)
        assert len(rows) == 1 and rows[0].settled_at is None and not ledger
        pending = await driver.read_current_title_result(service=service, running=running, required_work=work)
        assert not pending.settled and pending.settlement.result.reference.codec == "composer.title-provider-result.v1"
        work.coordinator.assert_completed()
        monkeypatch.setattr(sql_owner, "_append", original_append)
        monkeypatch.setattr(type(operation_store[3]), "finish_provider_attempt", original_finish)
        replay_work = inventory_work(running)
        complete = await driver.settle_current_title_result(service=service, running=running, required_work=replay_work)
        assert complete.settled and len(sdk) == 1
        assert len(sql_rows(operation_store)[1]) == 1
        replay_work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("cancel", "error"))
async def test_title_original_physical_failure_settles_once_and_unresolved_cut_never_retries_sdk(
    operation_store, tmp_path, monkeypatch, failure
):
    running, service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    original = asyncio.CancelledError("original SDK cancellation") if failure == "cancel" else RuntimeError("original SDK failure")
    sdk = []

    async def completion(**kwargs):
        sdk.append(kwargs)
        raise original

    monkeypatch.setattr(litellm, "acompletion", completion)
    calls = observer(monkeypatch)
    try:
        with pytest.raises(BaseException) as caught:
            await driver.run_prepared_title_provider(
                prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
            )
        assert caught.value is original
        assert len(calls) == len(sdk) == 1
        assert calls[0].private_request == capture_provider_context(
            messages=detach_prompt_guard_messages(prepared.request.audit_messages)[0], tools=None
        )
        assert calls[0].status.value == ("cancelled" if failure == "cancel" else "api_error")
        rows, ledger, _saved = sql_rows(operation_store)
        assert len(rows) == len(ledger) == 1
        assert rows[0].settled_at is not None
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="unresolved"):
            await driver.read_current_title_result(service=service, running=running, required_work=work)
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_real_quota_refusal_retains_preparation_but_never_physically_dispatches(operation_store, tmp_path, monkeypatch):
    running, _service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    from elspeth.web.sessions.models import identity_roles_table

    # The actual role owner is mutable; the immutable job/request is untouched.
    with operation_store[0].begin() as conn:
        from elspeth.web.coordination.database_clock import database_now

        conn.execute(
            update(identity_roles_table).where(identity_roles_table.c.identity_id == "alice").values(revoked_at=database_now(conn))
        )
    sdk = []

    async def completion(**kwargs):
        sdk.append(kwargs)
        raise AssertionError("refused TITLE reached the physical SDK")

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        assert (
            await driver.run_prepared_title_provider(
                prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
            )
            is None
        )
        assert not sdk and not sql_rows(operation_store)[0]
        assert operation_store[2].load_continuation_snapshot(running) == prepared.selected
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_two_current_writers_have_one_accounting_commit(operation_store, tmp_path, monkeypatch):
    running, service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    reached, resume = asyncio.Event(), asyncio.Event()
    original_finish = type(operation_store[3]).finish_provider_attempt

    async def pause(self, **kwargs):
        if kwargs["required_work"].key.authority is work.coordinator.authority:
            reached.set()
            await resume.wait()
        return await original_finish(self, **kwargs)

    monkeypatch.setattr(type(operation_store[3]), "finish_provider_attempt", pause)
    monkeypatch.setattr(litellm, "acompletion", lambda **kwargs: asyncio.sleep(0, result=raw_reply(kwargs["model"])))
    task = asyncio.create_task(
        driver.run_prepared_title_provider(
            prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        first_work, second_work = inventory_work(running), inventory_work(running)
        first = await driver.read_current_title_result(service=service, running=running, required_work=first_work)
        second = await driver.read_current_title_result(service=service, running=running, required_work=second_work)

        async def finish(current, binding):
            from elspeth.web.required_work import RequiredWorkSource

            ticket = binding.coordinator.reserve(
                RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL,
                transition_ordinal=binding.transition_ordinal,
                semantic_ordinal=6,
                recurrence_ordinal=0,
            )
            return await operation_store[3].finish_provider_attempt(
                session_operation_context=running.session_operation_context,
                call=current.settlement.audit_call,
                required_work=ticket,
                continuation_title=current.settlement,
            )

        outcomes = await asyncio.gather(finish(first, first_work), finish(second, second_work), return_exceptions=True)
        assert sum(outcome is None for outcome in outcomes) == 1
        assert sum(isinstance(outcome, ContinuationCASConflict) for outcome in outcomes) == 1
        assert len(sql_rows(operation_store)[1]) == 1
        first_work.coordinator.assert_completed()
        second_work.coordinator.assert_completed()
    finally:
        resume.set()
        with pytest.raises(ContinuationCASConflict):
            await task
        work.coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", (False, True))
async def test_title_post_retention_refresh_failure_keeps_original_and_closes_registered_work_before_replay(
    operation_store, tmp_path, monkeypatch, cancel
):
    from elspeth.web.required_work import RequiredWorkSource

    running, service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    original_refresh = driver.refresh_current_title_settlement
    marker = asyncio.CancelledError("original source read cancellation") if cancel else AuditIntegrityError("original source read refusal")
    read_results = []

    async def fail_after_real_current_read(custody):
        result = await original_refresh(custody)
        read_results.append(result)
        raise marker

    monkeypatch.setattr(driver, "refresh_current_title_settlement", fail_after_real_current_read)
    monkeypatch.setattr(litellm, "acompletion", lambda **kwargs: asyncio.sleep(0, result=raw_reply(kwargs["model"])))
    try:
        with pytest.raises(BaseException) as caught:
            await driver.run_prepared_title_provider(
                prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
            )
        assert caught.value is marker and len(read_results) == 1
        assert not [
            ticket for ticket in work.coordinator._tickets.values() if ticket.key.source is RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL
        ]
        work.coordinator.assert_completed()
        rows, ledger, _saved = sql_rows(operation_store)
        assert len(rows) == 1 and not ledger and rows[0].settled_at is None
        monkeypatch.setattr(driver, "refresh_current_title_settlement", original_refresh)
        replay = inventory_work(running)
        assert (await driver.settle_current_title_result(service=service, running=running, required_work=replay)).settled
        assert len(sql_rows(operation_store)[1]) == 1
        replay.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


async def actual_title_update(store, running, work, title):
    from elspeth.web.required_work import RequiredWorkSource

    sql, projection = work.reserve_pair(RequiredWorkSource.TITLE_ACCOUNTING_SQL, RequiredWorkSource.TITLE_ACCOUNTING_PROJECTION)
    result = await store[3].update_session_title(
        store[4], title, session_operation_context=running.session_operation_context, required_work=sql
    )
    projection.begin_projection()
    projection.complete_owned()
    return result


@pytest.mark.asyncio
async def test_title_new_dispatch_rechecks_actual_title_eligibility_without_blessing_old_preparation(
    operation_store, tmp_path, monkeypatch
):
    running, _service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    await actual_title_update(operation_store, running, work, "Current Owner Renamed This")
    sdk = []
    original_admit = type(operation_store[3]).begin_continuation_title_attempt
    owner_snapshots = []

    async def inspect_actual_admission(self, **kwargs):
        before = recovery_rows(operation_store[0])
        try:
            return await original_admit(self, **kwargs)
        finally:
            after = recovery_rows(operation_store[0])
            owner_snapshots.append((before, after))
            assert after == before

    monkeypatch.setattr(type(operation_store[3]), "begin_continuation_title_attempt", inspect_actual_admission)

    async def completion(**kwargs):
        sdk.append(kwargs)
        raise AssertionError("obsolete title eligibility reached SDK")

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        with pytest.raises(AuditIntegrityError):
            await driver.run_prepared_title_provider(
                prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
            )
        assert not sdk and not sql_rows(operation_store)[0]
        # Required prompt-guard audit may be appended before quota/source
        # admission; the continuation source/effect/root must remain unchanged.
        assert operation_store[2].load_continuation_snapshot(running) == prepared.selected
        assert len(owner_snapshots) == 1 and owner_snapshots[0][0] == owner_snapshots[0][1]
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_quota_clock_post_return_expiry_disposes_only_actual_known_undispatched_attempt(operation_store, tmp_path, monkeypatch):
    running, _service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    original_admit = ProviderCallCustody.admit
    admitted = []
    sdk = []

    async def complete_then_block(self, *, model):
        await original_admit(self, model=model)
        assert self.attempt is not None
        admitted.append(self.attempt)
        # Exercise the real clock's post-return check: synchronous work delays
        # returning without delivering asyncio's timeout into admit. This spends
        # the unchanged retained quota allowance; no timer/lease is enlarged.
        sleep(prepared.clock.policy.quota + 0.02)

    async def completion(**kwargs):
        sdk.append(kwargs)
        raise AssertionError("expired quota stage reached SDK")

    monkeypatch.setattr(ProviderCallCustody, "admit", complete_then_block)
    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        assert (
            await driver.run_prepared_title_provider(
                prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
            )
            is None
        )
        assert len(admitted) == 1 and not sdk
        rows, ledger, saved = sql_rows(operation_store)
        assert len(rows) == len(ledger) == 1 and rows[0].settled_at is not None
        assert ledger[0].prompt_tokens == ledger[0].completion_tokens == 0
        assert not [envelope for row in saved for envelope in row.tool_calls or () if envelope.get("_kind") == "llm_call_audit"]
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_title_cancellation_during_real_response_retention_joins_commit_and_original_finisher(operation_store, tmp_path, monkeypatch):
    running, service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    reached, resume = Event(), Event()
    original_append = sql_owner._append
    calls = observer(monkeypatch)
    sdk = []

    def pause_received(conn, running, selected, following, descriptor, stage):
        result = original_append(conn, running, selected, following, descriptor, stage)
        if stage == "received":
            reached.set()
            assert resume.wait(timeout=10)
        return result

    async def completion(**kwargs):
        sdk.append(kwargs)
        return raw_reply(kwargs["model"])

    monkeypatch.setattr(sql_owner, "_append", pause_received)
    monkeypatch.setattr(litellm, "acompletion", completion)
    task = asyncio.create_task(
        driver.run_prepared_title_provider(
            prepared=prepared, invocation_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work)
        )
    )
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        task.cancel("original caller cancellation during retained response commit")
        resume.set()
        with pytest.raises(asyncio.CancelledError, match="original caller cancellation"):
            await task
        assert len(sdk) == len(calls) == 1 and calls[0].status.value == "success"
        current = await driver.read_current_title_result(service=service, running=running, required_work=work)
        assert current.settled and current.settlement.audit_call == calls[0]
        assert not decode_continuation_snapshot(current.selected.payload_json).pending_settlement_ids
        assert len(sql_rows(operation_store)[1]) == 1
        work.coordinator.assert_completed()
    finally:
        resume.set()
        if not task.done():
            await task
        operation_store[1].release(running.session_operation_context)


def all_rows_on_connection(conn):
    from elspeth.web.sessions.models import metadata

    return tuple(
        (table.name, tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())) for table in metadata.sorted_tables
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("source_payload", "source_identity", "source_is_current", "cleared_current"))
async def test_prepared_title_ingress_uses_actual_published_anchor_and_current_root_without_writes(
    operation_store, tmp_path, monkeypatch, mutation
):
    from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
    from elspeth.web.sessions.continuation_ingress import read_prepared_title_ingress_on_connection

    running, _service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    try:
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            sql_owner._current(conn, running, prepared.selected)
            _descriptor, _ref, stage, source = sql_owner._source_chain(conn, running, prepared.selected, names=prepared.names)
            assert stage == "prepared" and source.cursor + 1 == prepared.selected.cursor
            source_frame = decode_continuation_snapshot(source.payload_json).frames[0]
            assert source_frame.bootstrap is not None
            values = sql_owner.bootstrap_values(
                sql_owner.decode_continuation_bootstrap(sql_owner._chunk(conn, running, source_frame.bootstrap))
            )
            arguments = {
                "actual": prepared.selected,
                "source": source,
                "content": values.request.content,
                "requested_state_id": values.request.state_id,
                "composition_state_id": values.request.state_id,
            }
            before = all_rows_on_connection(conn)
            actual = read_prepared_title_ingress_on_connection(conn, running, **arguments)
            assert all_rows_on_connection(conn) == before
            assert actual.snapshot is prepared.selected and actual.transcript == values.records
            if mutation == "source_payload":
                source_root = decode_continuation_snapshot(source.payload_json)
                altered = source_root.model_copy(update={"cursor": source_root.cursor + 1})
                arguments["source"] = replace(source, payload_json=encode_continuation_snapshot(altered))
            elif mutation == "source_identity":
                arguments["source"] = replace(source, checkpoint_id=prepared.selected.checkpoint_id)
            elif mutation == "source_is_current":
                arguments["source"] = prepared.selected
            else:
                root = decode_continuation_snapshot(prepared.selected.payload_json)
                frame = root.frames[0].model_copy(update={"provider_effect_key": None, "provider_input_sha256": None})
                arguments["actual"] = replace(
                    prepared.selected, payload_json=encode_continuation_snapshot(root.model_copy(update={"frames": (frame,)}))
                )
            before = all_rows_on_connection(conn)
            with pytest.raises((AuditIntegrityError, ContinuationCASConflict)):
                read_prepared_title_ingress_on_connection(conn, running, **arguments)
            assert all_rows_on_connection(conn) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_prepared_title_ingress_rejects_extra_actual_cas_transition_after_genuine_positive(operation_store, tmp_path, monkeypatch):
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot
    from elspeth.web.sessions.continuation_ingress import read_prepared_title_ingress_on_connection

    running, _service, work, prepared, _requests = await title_case(operation_store, tmp_path, monkeypatch)
    try:
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            sql_owner._current(conn, running, prepared.selected)
            _descriptor, _ref, _stage, source = sql_owner._source_chain(conn, running, prepared.selected, names=prepared.names)
            source_frame = decode_continuation_snapshot(source.payload_json).frames[0]
            assert source_frame.bootstrap is not None
            values = sql_owner.bootstrap_values(
                sql_owner.decode_continuation_bootstrap(sql_owner._chunk(conn, running, source_frame.bootstrap))
            )
            arguments = {
                "source": source,
                "content": values.request.content,
                "requested_state_id": values.request.state_id,
                "composition_state_id": values.request.state_id,
            }
            assert (
                read_prepared_title_ingress_on_connection(conn, running, actual=prepared.selected, **arguments).snapshot
                is prepared.selected
            )
            # A real current CAS is fixture setup, with every prepared TITLE
            # field retained. The reader must refuse this unsupported extra cut.
            extra = append_complete_snapshot(
                conn,
                session_id=str(running.claim.session_id),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=prepared.selected.checkpoint_id,
                expected_cursor=prepared.selected.cursor,
                snapshot_json=prepared.selected.payload_json,
                event_batch=(ContinuationEvent("checkpoint", canonical_json({"fixture": "unowned prepared extension"})),),
                handoff=False,
            )
            before = all_rows_on_connection(conn)
            with pytest.raises(AuditIntegrityError, match="full actual forward source transition"):
                read_prepared_title_ingress_on_connection(conn, running, actual=extra, **arguments)
            assert all_rows_on_connection(conn) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
