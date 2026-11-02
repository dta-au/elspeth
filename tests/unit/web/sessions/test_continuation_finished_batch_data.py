"""A real applied-P4 cold read exposes only post-batch DATA."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_finished_batch_data import (
    ColdFinishedBatchData,
    ColdPostBatchPersistData,
    project_cold_finished_batch_data,
)
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.sessions import continuation_iteration_return as stages
from elspeth.web.sessions.continuation_p4_application_store import StoredP4Application
from elspeth.web.sessions.continuation_primary import RetainedPrimaryPhaseData
from elspeth.web.sessions.locking import locked_session_transaction
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case, stored_rows
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
async def test_actual_applied_p4_cold_batch_projects_data_without_writer_or_provider_replay(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (warm, running, primary, sdk):
        for _ in range(len(primary.outcome.completion.tool_batch.calls) + 1):
            await _advance(warm)
        assert warm.p4 is not None and type(warm.p4.application) is StoredP4Application
        selected = await load_selected_continuation(
            authority=warm.authority,
            running=running,
            selected=warm.p4.application.snapshot,
            required_work=warm.work,
        )
        cold = ContinuationLivePhases(warm.service, warm.authority, warm.work, warm.provider_owner, warm.lease)
        runtime, inventory = await cold.current(selected)
        paid = await cold._primary_for_phase(runtime, inventory, selected)
        assert type(paid) is RetainedPrimaryPhaseData
        context = await cold._tool_context(runtime)
        before = stored_rows(operation_store[0])
        original_p4 = warm.p4.application.bundle.result
        assert runtime.live.persisted_assistant_message_id != original_p4.outcome.assistant_id

        result = project_cold_finished_batch_data(
            runtime=runtime,
            selected=selected,
            primary=paid,
            p4_result=original_p4,
            iteration_stage=None,
            context=context,
        )

        assert type(result) is ColdFinishedBatchData and type(result.persist) is ColdPostBatchPersistData
        assert runtime.graph.batch is not None
        assert result.pre_batch_state is runtime.graph.batch.batch.entry_state
        assert result.dispatch.state is runtime.graph.batch.batch.state
        assert result.dispatch.assistant_tool_calls is runtime.graph.batch.calls
        assert result.persist.persisted_assistant_message_id == str(warm.p4.application.bundle.result.receipt.assistant_id)
        assert result.persist.failed_turn.tool_calls_attempted == len(runtime.graph.batch.calls)
        assert result.persist.failed_turn.tool_responses_persisted == len(runtime.graph.batch.batch.tool_outcomes)
        assert result.persist.effect_verification_failure is None and result.persist.authority_revocation is None
        assert len(sdk) == 1 and stored_rows(operation_store[0]) == before
    warm.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_applied_p4_cold_projection_refuses_foreign_receipt_without_new_effects(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (warm, running, primary, sdk):
        for _ in range(len(primary.outcome.completion.tool_batch.calls) + 1):
            await _advance(warm)
        assert warm.p4 is not None and type(warm.p4.application) is StoredP4Application
        selected = await load_selected_continuation(
            authority=warm.authority,
            running=running,
            selected=warm.p4.application.snapshot,
            required_work=warm.work,
        )
        cold = ContinuationLivePhases(warm.service, warm.authority, warm.work, warm.provider_owner, warm.lease)
        runtime, inventory = await cold.current(selected)
        paid = await cold._primary_for_phase(runtime, inventory, selected)
        assert type(paid) is RetainedPrimaryPhaseData
        context = await cold._tool_context(runtime)
        actual = warm.p4.application.bundle.result
        foreign = replace(actual, receipt=actual.receipt.model_copy(update={"effect_key": "batch:foreign:p4"}))
        before = stored_rows(operation_store[0])

        with pytest.raises(AuditIntegrityError, match="complete P4 receipt"):
            project_cold_finished_batch_data(
                runtime=runtime,
                selected=selected,
                primary=paid,
                p4_result=foreign,
                iteration_stage=None,
                context=context,
            )
        assert len(sdk) == 1 and stored_rows(operation_store[0]) == before
    warm.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_cold_p4_projection_compares_fold_fields_only_for_actual_stage_source(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (warm, running, primary, sdk):
        for _ in range(len(primary.outcome.completion.tool_batch.calls) + 1):
            await _advance(warm)
        assert warm.p4 is not None and type(warm.p4.application) is StoredP4Application
        raw = await load_selected_continuation(
            authority=warm.authority, running=running, selected=warm.p4.application.snapshot, required_work=warm.work
        )
        cold = ContinuationLivePhases(warm.service, warm.authority, warm.work, warm.provider_owner, warm.lease)
        raw_runtime, raw_inventory = await cold.current(raw)
        raw_paid = await cold._primary_for_phase(raw_runtime, raw_inventory, raw)
        assert type(raw_paid) is RetainedPrimaryPhaseData
        raw_context = await cold._tool_context(raw_runtime)
        captured = await capture_continuation_runtime_owners(raw_runtime, selected=raw, inventory=raw_inventory)
        prepared = stages.retain_iteration_prepared(warm.authority, running, selected=raw, inventory=raw_inventory, captured=captured)
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            stage = stages.read_iteration_stage_on_connection(conn, running, selected=prepared)
        assert stage is not None and stage.stage in ("early_ready", "classify_ready")
        before = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="proved post-P4 fold"):
            project_cold_finished_batch_data(
                runtime=raw_runtime,
                selected=raw,
                primary=raw_paid,
                p4_result=warm.p4.application.bundle.result,
                iteration_stage=stage,
                context=raw_context,
            )
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
    warm.work.coordinator.assert_completed()
