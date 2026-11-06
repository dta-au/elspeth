"""Actual V4 empty cold producer; nonempty/resource work stays refused."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from operator import setitem
from threading import Event, get_ident

import litellm
import pytest
from sqlalchemy import event

from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_value_batch_preview as preview
from elspeth.web.composer.composer_preflight import ComposerPreflight
from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data, encode_prepared_batch_data
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_runtime import (
    build_runtime_tool_batch_context,
    restore_metadata_preparation_runtime,
)
from elspeth.web.composer.continuation_value_batch_preparation import PreparedValueBatchCall, ValueBatchPreparationRefused
from elspeth.web.composer.protocol import ComposerRuntimePreflightError
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.state import SourceSpec
from elspeth.web.composer.tool_batch import ContinuationCallState, _prepare_continuation_call_live
from elspeth.web.execution import validation as runtime_validation
from tests.unit.web.composer.test_runtime_preflight_pending_review_verification import _handoff_result
from tests.unit.web.sessions import test_continuation_live_phases as phases
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows
from tests.unit.web.sessions.test_continuation_v4_metadata_live import _v4_tool_case

operation_store = phases.operation_store


@asynccontextmanager
async def prepared_preview(store, tmp_path, monkeypatch):
    # Compose the canonical real PRIMARY/phase/runtime owners. Cold cache
    # observations remain history; no retained verdict is reminted as active.
    async with _v4_tool_case(store, tmp_path, monkeypatch, tool="preview_pipeline", arguments={}) as (
        current_store,
        owner,
        running,
        sdk,
    ):
        selected, inventory = await phases._finish_inventory(owner, running)
        original, inventory = await owner.current(selected)
        assert not original.live.state.sources and not original.live.state.nodes and not original.live.state.outputs
        provider = ProviderInvocationOwner(service=current_store[3], required_work=owner.work)
        detached = await restore_metadata_preparation_runtime(
            service=owner.service,
            authority=owner.authority,
            selected=selected,
            inventory=inventory,
            required_work=owner.work,
            provider_owner=provider,
        )
        assert detached.live.runtime_preflight_cache is not original.live.runtime_preflight_cache
        phase = ContinuationLivePhases(owner.service, owner.authority, owner.work, provider, owner.lease, runtime=detached)
        primary = await phase._primary_for_phase(detached, inventory, selected)
        context = await build_runtime_tool_batch_context(detached, cancellation_requested=owner.cancellation_requested)
        data = detached.graph.batch
        assert data is not None and data.batch.phase == "call_ready"
        call = ContinuationCallState(0, data.calls[0], data.batch.state, data.batch.state.version)
        data.batch.current_call, data.batch.phase = call, "call_active"
        status = await _prepare_continuation_call_live(
            call_model=primary.outcome, ctx=context, llm_messages=detached.live.llm_messages, batch=data.batch, call=call
        )
        assert status == "prepared" and call.audit is not None
        encoded = encode_prepared_batch_data(
            batch=data.batch,
            accumulator=data.accumulator,
            calls=data.calls,
            dialect=context.tool_contract_dialect,
            env_ref_names=detached.env_ref_names,
        )
        portable = decode_prepared_batch_data(encoded, env_ref_names=detached.env_ref_names).audit
        prepared = PreparedValueBatchCall(primary.outcome, context, detached, call, encoded, call.audit, replace(call.audit), portable)
        plan = preview.plan_preview_value_source(prepared)
        assert plan.cached is None and plan.key not in prepared.context.runtime_preflight_cache
        yield current_store, original, prepared, sdk


def forbid_new_resources(monkeypatch):
    from elspeth.web.composer.tools import generation

    entered = []

    def forbidden(*args, **kwargs):
        entered.append(1)
        raise AssertionError("empty preview entered plugin, settings, source or provider work")

    monkeypatch.setattr(runtime_validation, "instantiate_runtime_plugins", forbidden)
    monkeypatch.setattr(runtime_validation, "load_settings_from_config_dict", forbidden)
    monkeypatch.setattr(runtime_validation, "load_settings_from_yaml_string", forbidden)
    monkeypatch.setattr(runtime_validation, "load_bounded_pipeline_yaml", forbidden)
    monkeypatch.setattr(generation, "_locked_read_ready_blob", forbidden)
    monkeypatch.setattr(litellm, "acompletion", forbidden)
    return entered


@pytest.mark.asyncio
async def test_actual_cold_empty_preview_returns_fixed_original_verdict_and_result_without_resource_reads(
    operation_store, tmp_path, monkeypatch
):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (store, original, prepared, sdk):
        plan = preview.plan_preview_value_source(prepared)
        assert plan.arguments == {} and prepared.call.arguments == {}
        with pytest.raises(TypeError):
            setitem(plan.arguments, "changed", True)
        with pytest.raises(FrozenInstanceError):
            plan.cached_json = "changed"
        assert prepared.call.arguments == {}
        cache_before = dict(prepared.context.runtime_preflight_cache)
        original_cache = dict(original.live.runtime_preflight_cache)
        rows_before = stored_rows(store[0])
        invocation_before = prepared.context.recorder.invocations
        actual_dispatch = preview.execute_tool
        actual_preflight = ComposerPreflight.runtime_preflight
        returned = []
        produced = []
        owned_threads = {get_ident()}
        sql = []

        def observe(*args, **kwargs):
            owned_threads.add(get_ident())
            value = actual_dispatch(*args, **kwargs)
            returned.append(value)
            return value

        def observe_preflight(self, *args, **kwargs):
            owned_threads.add(get_ident())
            value = actual_preflight(self, *args, **kwargs)
            produced.append(value)
            return value

        def observe_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            if get_ident() in owned_threads:
                if statement == "BEGIN":
                    sql.append("deferred_read_begin")
                elif statement == "SELECT 1":
                    sql.append("control_select")
                else:
                    sql.append("unexpected_statement")

        monkeypatch.setattr(preview, "execute_tool", observe)
        monkeypatch.setattr(ComposerPreflight, "runtime_preflight", observe_preflight)
        external = forbid_new_resources(monkeypatch)
        event.listen(store[0], "before_cursor_execute", observe_sql)
        try:
            # Control the observer before using its empty result as evidence.
            with store[0].connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            # The canonical SQLite engine's begin listener emits its deferred
            # BEGIN on plain connect before this explicit control SELECT.
            assert sql == ["deferred_read_begin", "control_select"]
            sql.clear()

            def foreign_read():
                with store[0].connect() as conn:
                    conn.exec_driver_sql("SELECT 1")

            await asyncio.to_thread(foreign_read)
            assert not sql
            computed = await preview.compute_preview_value(prepared)
            assert not sql
        finally:
            event.remove(store[0], "before_cursor_execute", observe_sql)
        assert returned == [computed.result] and computed.result is returned[0]
        assert computed.result.success is True and computed.result.updated_state is prepared.call.pre_state
        assert len(produced) == 1 and computed.result.runtime_preflight is produced[0]
        assert produced[0].is_valid is False and produced[0].errors[0].error_code == "empty_pipeline"
        assert computed.result.data["preview_is_valid"] is False
        assert computed.result.data["proof_diagnostics"] == ()
        assert computed.source_chunks == ()
        assert prepared.context.runtime_preflight_cache == {**cache_before, plan.key: produced[0]}
        assert original.live.runtime_preflight_cache == original_cache
        assert prepared.context.recorder.invocations == invocation_before
        assert stored_rows(store[0]) == rows_before and not external and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("missing", "session_scope", "state_version", "state_content_hash", "settings_hash", "tolerant"))
async def test_empty_preview_produces_current_verdict_instead_of_reusing_foreign_cache(operation_store, tmp_path, monkeypatch, changed):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_store, _original, prepared, _sdk):
        plan = preview.plan_preview_value_source(prepared)
        ctx = prepared.context
        cache = prepared.context.runtime_preflight_cache
        foreign = await ctx.preflight.cached_runtime_preflight(
            prepared.call.pre_state,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            cache=cache,
            initial_version=ctx.initial_version,
            session_scope=ctx.session_scope,
            llm_calls=ctx.recorder.llm_calls,
            plugin_snapshot=ctx.plugin_snapshot,
            session_operation_context=ctx.session_operation_context,
        )
        assert cache.pop(plan.key) is foreign
        # The original real producer has completed; no in-flight coalescing
        # may masquerade as either a completed cache hit or a new invocation.
        await asyncio.sleep(0)
        assert plan.key not in ctx.preflight._coordinator._inflight
        foreign_json = canonical_json(foreign.model_dump(mode="json"))
        wrong_key = None
        if changed != "missing":
            change = {
                "session_scope": {"session_scope": plan.key.session_scope + ":other"},
                "state_version": {"state_version": plan.key.state_version + 1},
                "state_content_hash": {"state_content_hash": "0" * 64},
                "settings_hash": {"settings_hash": plan.key.settings_hash + ":other"},
                "tolerant": {"interpretation_tolerant": True},
            }[changed]
            wrong_key = replace(plan.key, **change)
            assert wrong_key != plan.key
            # Adversarial cache-key DATA is never accepted as the current key.
            cache[wrong_key] = foreign
        actual = ComposerPreflight.runtime_preflight
        produced = []

        def observe(self, *args, **kwargs):
            result = actual(self, *args, **kwargs)
            produced.append(result)
            return result

        monkeypatch.setattr(ComposerPreflight, "runtime_preflight", observe)
        external = forbid_new_resources(monkeypatch)
        computed = await preview.compute_preview_value(prepared)
        assert len(produced) == 1 and computed.result.runtime_preflight is produced[0]
        assert produced[0] is not foreign and cache[plan.key] is produced[0]
        assert canonical_json(foreign.model_dump(mode="json")) == foreign_json and not external
        if wrong_key is not None:
            assert cache[wrong_key] is foreign


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("source", "pending", "cancelled", "retained"))
async def test_preview_refuses_unbounded_or_already_retained_branch_before_work(operation_store, tmp_path, monkeypatch, changed):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_store, _original, prepared, _sdk):
        plan = preview.plan_preview_value_source(prepared)
        if changed == "source":
            # Adversarial DATA only: no forged graph/source is ever admitted or
            # dispatched. The additional component must hit the preissue stop.
            state = prepared.call.pre_state.with_source(SourceSpec("csv", "output", {"path": "must-not-be-opened.csv"}, "discard"))
            prepared.call.pre_state = state
            plan.data.batch.state = state
        elif changed == "pending":
            prepared.context.runtime_preflight_cache[plan.key] = _handoff_result()
        elif changed == "cancelled":
            prepared.context.cancellation_requested.set()
        else:
            prepared.call.phase = "application_pending"
        entered = []

        async def forbidden(*args, **kwargs):
            entered.append(1)
            raise AssertionError("unsupported preview branch entered producer work")

        monkeypatch.setattr(ComposerPreflight, "cached_runtime_preflight", forbidden)
        monkeypatch.setattr(preview, "run_sync_in_worker", forbidden)
        with pytest.raises(ValueBatchPreparationRefused):
            await preview.compute_preview_value(prepared)
        assert not entered


@pytest.mark.asyncio
async def test_preview_preserves_original_dispatch_failure(operation_store, tmp_path, monkeypatch):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_store, _original, prepared, _sdk):
        original = RuntimeError("original empty-preview dispatcher defect")

        def crash(*args, **kwargs):
            raise original

        monkeypatch.setattr(preview, "execute_tool", crash)
        with pytest.raises(RuntimeError) as caught:
            await preview.compute_preview_value(prepared)
        assert caught.value is original


@pytest.mark.asyncio
async def test_empty_preview_preserves_original_preflight_failure_without_dispatch(operation_store, tmp_path, monkeypatch):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_store, _original, prepared, _sdk):
        original = RuntimeError("original empty-preflight defect")
        dispatched = []

        def crash(*args, **kwargs):
            raise original

        def forbidden(*args, **kwargs):
            dispatched.append(1)
            raise AssertionError("failed original preflight entered preview dispatch")

        monkeypatch.setattr(ComposerPreflight, "runtime_preflight", crash)
        monkeypatch.setattr(preview, "execute_tool", forbidden)
        with pytest.raises(ComposerRuntimePreflightError) as caught:
            await preview.compute_preview_value(prepared)
        assert caught.value.original_exc is original and not dispatched


@pytest.mark.asyncio
async def test_preview_preserves_original_cancellation_before_dispatch(operation_store, tmp_path, monkeypatch):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_store, _original, prepared, _sdk):
        entered, release = Event(), Event()
        observed = []
        dispatched = []
        actual_preflight = ComposerPreflight.runtime_preflight
        actual_cached = ComposerPreflight.cached_runtime_preflight

        async def observe_cancel(self, *args, **kwargs):
            try:
                return await actual_cached(self, *args, **kwargs)
            except asyncio.CancelledError as original:
                observed.append(original)
                raise

        def parked(self, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release the actual empty preflight")
            return actual_preflight(self, *args, **kwargs)

        def forbidden(*args, **kwargs):
            dispatched.append(1)
            raise AssertionError("cancelled preview entered its dispatcher")

        monkeypatch.setattr(ComposerPreflight, "runtime_preflight", parked)
        monkeypatch.setattr(ComposerPreflight, "cached_runtime_preflight", observe_cancel)
        monkeypatch.setattr(preview, "execute_tool", forbidden)
        task = asyncio.create_task(preview.compute_preview_value(prepared))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel("original preview caller")
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert observed == [caught.value] and observed[0] is caught.value and not dispatched
            workers = tuple(prepared.context.preflight._coordinator._inflight.values())
            assert len(workers) == 1 and not workers[0].done()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            workers = tuple(prepared.context.preflight._coordinator._inflight.values())
            await asyncio.gather(*workers, return_exceptions=True)
