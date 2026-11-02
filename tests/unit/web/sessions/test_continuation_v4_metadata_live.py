"""Real V4 PRIMARY → bounded metadata/P4, with unsupported-tool opponents."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace

import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import EffectFrontierEntry, ToolBatchFrame
from elspeth.web.composer.continuation_dispatch_audit import capture_dispatch_audit
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, capture_continuation_runtime_owners
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tool_batch import ContinuationCallState, _prepare_continuation_call_live
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_next_iteration import is_applied_batch_cut
from elspeth.web.sessions.continuation_tool_no_execution import ToolNoExecutionRefusal, read_v3_tool_no_execution_refusal_on_connection
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composition_states_table as states
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.sessions import test_continuation_initialization as bootstrap_fixture
from tests.unit.web.sessions import test_continuation_live_phases as phase_fixture
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows
from tests.unit.web.sessions.test_continuation_prompt_producers import current_service

operation_store = phase_fixture.operation_store


@asynccontextmanager
async def _v4_tool_case(
    store,
    tmp_path,
    monkeypatch,
    *,
    tool="set_metadata",
    arguments=None,
    following_tool_call=None,
    available_plugins=None,
    primary_preparation_probe_15s=False,
    retained_payload_bytes=16 * 1_048_576,
):
    from tests.helpers.continuation_policy import classifier_15s_probe_settings

    settings_factory = classifier_15s_probe_settings if primary_preparation_probe_15s else continuation_test_settings
    settings = settings_factory(composer_continuation_max_retained_payload_bytes=retained_payload_bytes)
    policy = continuation_policy_v4_from_settings(settings)
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    # The normal fixture's actual admitting owner retains this exact V4 seed;
    # no historical policy rewrite or fabricated paid response is used.
    monkeypatch.setattr(bootstrap_fixture, "minimal_continuation_policy", lambda: policy)
    if available_plugins is not None:

        def configured_service(current_store, *, secret_service=None, settings_overrides=None):
            return current_service(
                current_store,
                secret_service=secret_service,
                settings_overrides=settings_overrides,
                available_plugins=available_plugins,
            )

        # Configure the admitting service before bootstrap and PRIMARY. This
        # supplies its real policy/snapshot factory, never a retained rewrite.
        monkeypatch.setattr(bootstrap_fixture, "current_service", configured_service)
    current_store = (store[0], store[1], authority, store[3], store[4])
    if arguments is None:
        arguments = {"patch": {"name": "V4 original model metadata"}} if tool == "set_metadata" else {}
    async with phase_fixture._phase_case(
        current_store,
        tmp_path,
        monkeypatch,
        tool_call=("original_v4_tool", tool, arguments),
        following_tool_call=following_tool_call,
        settings_overrides={
            "composer_continuation_max_retained_payload_bytes": policy.max_retained_payload_bytes,
            **(
                {"composer_continuation_action_bounds": settings.composer_continuation_action_bounds}
                if primary_preparation_probe_15s
                else {}
            ),
        },
    ) as (owner, running, primary, sdk):
        assert len(sdk) == 1 and primary.outcome.completion.tool_batch.calls[0].function.name == tool
        assert owner.runtime is not None
        if available_plugins is not None:
            snapshot = owner.runtime.bindings.plugin_snapshot
            assert snapshot.available == available_plugins and owner.runtime.bindings.policy_catalog.snapshot is snapshot
        yield current_store, owner, running, sdk


def _forbid_execution(monkeypatch):
    from elspeth.web.composer import tool_batch

    entered = []

    async def forbidden(*args, **kwargs):
        entered.append((args, kwargs))
        raise AssertionError("bounded metadata/refusal entered generic execution or another provider")

    # The real live module imports the worker directly; forbid both routes.
    from elspeth.web.composer import continuation_live_phases

    monkeypatch.setattr(continuation_live_phases, "_execute_continuation_call_live", forbidden)
    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", forbidden)
    monkeypatch.setattr(litellm, "acompletion", forbidden)
    return entered


@pytest.mark.asyncio
async def test_actual_v4_primary_metadata_tail_applies_and_cold_restore_never_reexecutes(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, running, sdk):
        entered = _forbid_execution(monkeypatch)
        with store[0].connect() as conn:
            before = conn.execute(select(states).where(states.c.session_id == str(store[4]))).all()
        await phase_fixture._advance(owner)
        stored = owner.latest_snapshot
        assert stored is not None and owner.runtime is owner.primary is owner.p4 is owner.finished_batch is None
        with store[0].connect() as conn:
            published = conn.execute(select(states).where(states.c.session_id == str(store[4])).order_by(states.c.version)).all()
            actual_effects = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).all()
        assert len(published) == len(before) + 1
        assert published[-1].metadata_["data"]["name"] == "V4 original model metadata"
        assert all(row.disposition == "committed" for row in actual_effects)
        cold = ContinuationLivePhases(
            owner.service,
            owner.authority,
            owner.work,
            ProviderInvocationOwner(service=store[3], required_work=owner.work),
            owner.lease,
        )
        from elspeth.web.composer.continuation_selection import load_selected_continuation

        selected = await load_selected_continuation(authority=owner.authority, running=running, selected=stored, required_work=owner.work)
        assert is_applied_batch_cut(selected)
        rows_before = stored_rows(store[0])
        runtime, _inventory = await cold.current(selected)
        assert runtime.live.state.metadata.name == "V4 original model metadata"
        assert runtime.graph.batch is not None and runtime.live.state is runtime.graph.batch.batch.state
        assert stored_rows(store[0]) == rows_before and len(sdk) == 1 and not entered


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ("get_pipeline_state", "preview_pipeline"))
async def test_actual_v4_unsupported_tool_is_unissued_cold_terminal(operation_store, tmp_path, monkeypatch, tool):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch, tool=tool) as (store, owner, running, sdk):
        entered = _forbid_execution(monkeypatch)
        selected, inventory = await phase_fixture._finish_inventory(owner, running)
        with store[0].connect() as conn:
            before_states = conn.execute(select(states).where(states.c.session_id == str(store[4]))).all()
        receipt = await owner._execute_one(selected)
        assert type(receipt) is ToolNoExecutionRefusal
        with store[0].connect() as conn:
            cold = read_v3_tool_no_execution_refusal_on_connection(
                conn,
                session_id=store[4],
                operation_id=running.claim.operation_id,
                effect_key=receipt.effect_key,
                env_ref_names=inventory.names,
            )
            actual_states = conn.execute(select(states).where(states.c.session_id == str(store[4]))).all()
            parent = conn.execute(
                select(jobs).where(jobs.c.session_id == str(store[4]), jobs.c.operation_id == running.claim.operation_id)
            ).one()
        assert cold == receipt and parent.status == "failed"
        assert actual_states == before_states and len(sdk) == 1 and not entered


@pytest.mark.asyncio
async def test_actual_v4_generic_sql_issue_cannot_bypass_complete_tail_admission(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch, tool="get_pipeline_state") as (store, owner, _running, sdk):
        assert owner.runtime is not None
        selected = owner.runtime.source.selected
        runtime, inventory = await owner.current(selected)
        data = runtime.graph.batch
        assert data is not None
        original = await owner._primary_for_phase(runtime, inventory, selected)
        context = await owner._tool_context(runtime)
        call = ContinuationCallState(0, data.calls[0], data.batch.state, data.batch.state.version)
        data.batch.current_call, data.batch.phase = call, "call_active"
        assert (
            await _prepare_continuation_call_live(
                call_model=original.outcome, ctx=context, llm_messages=runtime.live.llm_messages, batch=data.batch, call=call
            )
            == "prepared"
        )
        assert call.audit is not None
        owner.runtime = runtime = replace(
            runtime,
            graph=runtime.graph.with_selected_dispatch(
                capture_dispatch_audit(call.audit, env_ref_names=inventory.names),
                context.tool_contract_dialect,
            ),
        )
        captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
        top = captured.stack.frames[-1]
        assert type(top) is ToolBatchFrame
        root = captured.stack.model_copy(
            update={
                "frames": (*captured.stack.frames[:-1], top.model_copy(update={"phase": "effect_reconciling"})),
                "effect_frontier": (
                    *captured.stack.effect_frontier,
                    EffectFrontierEntry(
                        effect_id=f"tool:{original.response.reference.digest}:0",
                        disposition="dispatched",
                        receipt_id=None,
                    ),
                ),
            }
        )
        before = stored_rows(store[0])
        with pytest.raises(AuditIntegrityError, match="complete finite result envelope"):
            await owner._retain(CapturedContinuationRuntime(selected, root, captured.chunks), inventory, "tool_issued")
        assert stored_rows(store[0]) == before and len(sdk) == 1
