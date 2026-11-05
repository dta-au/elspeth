"""Original V4 EARLY graph transition, request custody and receiving controls."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer import continuation_checkpoint_early_physical as physical_boundary
from elspeth.web.composer.advisor_invocation_outcome import _AdvisorInvocationCapture
from elspeth.web.composer.advisor_policy import advisor_arguments_with_format_reprompt
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.content_scope import composition_scope_for_operation
from elspeth.web.composer.continuation_checkpoint_early_observation import observe_v4_early_checkpoint_returned_response
from elspeth.web.composer.continuation_checkpoint_early_physical import (
    checkpoint_early_detached_runtime,
    checkpoint_early_format_retry,
    checkpoint_early_physical_custody,
    checkpoint_early_physical_preparation,
    require_checkpoint_early_prepared_clock,
    require_original_checkpoint_early_physical_source,
)
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prepared_provider_input import PreparedProviderInput
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.continuation_retention_memo import current_v4_preparation_memo
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions import continuation_iteration_return as stages
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_checkpoint_source_replay import read_checkpoint_provider_source_on_connection
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_app import install_clean_guard
from tests.helpers.continuation_early_profile import early_preparation_profile
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.sessions import test_continuation_composition_initialization as composition_fixture
from tests.unit.web.sessions import test_continuation_live_phases as phase_fixture
from tests.unit.web.sessions import test_continuation_v4_metadata_live as v4_fixture
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows
from tests.unit.web.sessions.test_continuation_prompt_producers import current_service
from tests.unit.web.sessions.test_continuation_v4_pipeline_live import _PIPELINE_PLUGINS, _arguments, _uploaded_source

operation_store = phase_fixture.operation_store


@asynccontextmanager
async def _early_case(store, tmp_path, monkeypatch):
    """Establish EARLY through the actual original PRIMARY, tool and P4 owners."""
    guard = install_clean_guard(monkeypatch)

    def guarded_service(current_store, *, secret_service=None, settings_overrides=None, available_plugins=None):
        return current_service(
            current_store,
            secret_service=secret_service,
            available_plugins=available_plugins,
            settings_overrides={
                **(settings_overrides or {}),
                "composer_advisor_model": "test/model",
                "composer_advisor_prompt_guard": guard,
                "composer_prompt_guard_mode": "required",
            },
        )

    def original_timed_work(running):
        # The existing phase fixture creates work before restoring the first
        # graph. Give that same coordinator its actual retained SQL timing.
        observed_at = time.monotonic()
        with locked_session_transaction(store[0], str(running.claim.session_id)) as conn:
            selected = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            root = decode_continuation_snapshot(selected.payload_json)
            policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
            deadline_at = conn.execute(
                select(segments.c.deadline_at).where(
                    segments.c.session_id == str(running.claim.session_id),
                    segments.c.operation_id == running.claim.operation_id,
                    segments.c.sequence == running.claim.segment_sequence,
                    segments.c.generation == running.claim.ownership_generation,
                )
            ).scalar_one()
            remaining = (restore_utc(deadline_at) - database_now(conn)).total_seconds()
        deadline = ContinuationSQLDeadlineOwner(
            running,
            policy=policy,
            drain_signal=ComposerDrainSignal(),
            drain_seconds=10.0,
            hard_expires_at=observed_at + remaining,
        )
        return inventory_work(running, sql_deadline_owner=deadline)

    monkeypatch.setattr(v4_fixture, "current_service", guarded_service)
    monkeypatch.setattr(composition_fixture, "inventory_work", original_timed_work)
    blob = await _uploaded_source(store)
    output = Path(store[3]._data_dir) / "outputs" / str(store[4]) / "original-early-output.json"
    async with v4_fixture._v4_tool_case(
        store,
        tmp_path,
        monkeypatch,
        tool="set_pipeline",
        arguments=_arguments(blob, output),
        available_plugins=_PIPELINE_PLUGINS,
    ) as (current_store, owner, running, primary_sdk):
        await phase_fixture._advance(owner)
        applied = owner.latest_snapshot
        assert applied is not None and owner.tool_refusal is None
        selected = await load_selected_continuation(authority=owner.authority, running=running, selected=applied, required_work=owner.work)
        phase_read = await owner.restore_finished_batch(selected)
        runtime, inventory = phase_read.runtime, phase_read.inventory
        captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
        ready = stages.retain_iteration_prepared(owner.authority, running, selected=selected, inventory=inventory, captured=captured)
        selected = await load_selected_continuation(authority=owner.authority, running=running, selected=ready, required_work=owner.work)
        runtime, inventory = await owner.current(selected)
        dispatched = stages.begin_iteration_paid_stage(owner.authority, running, selected=selected, inventory=inventory, stage="early")
        selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=dispatched, required_work=owner.work
        )
        runtime, _inventory = await owner.current(selected)
        assert owner.finished_batch is not None and len(primary_sdk) == 1
        with early_preparation_profile(monkeypatch, store[0], output_key=f"{running.claim.session_id}-{running.claim.operation_id}"):
            clock = ContinuationActionClock(runtime.source.policy.actions.advisor)
            deadline = owner.work.coordinator.sql_deadline_owner
            assert deadline is not None
            deadline.begin_action(clock.policy, started_at=clock.started_monotonic)
            owner.bind_action_clock(clock)
            yield current_store, owner, running, selected, primary_sdk


@asynccontextmanager
async def _early_attempt(owner, selected):
    runtime = owner.runtime
    assert runtime is not None and runtime.source.selected is selected and owner.finished_batch is not None
    dispatch, persist, before = owner.finished_batch
    async with checkpoint_early_physical_preparation(
        owner, runtime, selected=selected, dispatch=dispatch, persist=persist, pre_batch_state=before
    ) as source:
        assert current_v4_preparation_memo(source.clock) is not None
        yield source


def _early_arguments(source):
    owner = source.phase_owner
    bindings, runtime = owner.runtime.bindings, owner.runtime
    advisor = owner.phases.service._advisor_checkpoint
    scope = composition_scope_for_operation(
        runtime.live.state,
        session_id=bindings.session_id,
        session_operation_context=bindings.session_operation_context,
        session_operation_authority=advisor._require_sessions_service().session_operation_authority,
    )
    arguments = advisor._build_checkpoint_arguments(
        phase="early",
        state=runtime.live.state,
        content_scope=scope,
        env_ref_names=runtime.env_ref_names,
        advisor_transcript=runtime.live.turn_evidence.transcript.snapshot(),
    )
    retry = checkpoint_early_format_retry(source)
    if retry is not None and retry.marker.format_reprompt_required:
        return advisor_arguments_with_format_reprompt(arguments, schema_valid=retry.marker.first_attempt_schema_valid is True)
    return arguments


async def _invoke_early(source):
    owner = source.phase_owner
    return await owner.phases.service._advisor_checkpoint._call_advisor_with_audit(
        _early_arguments(source),
        recorder=owner.runtime.live.recorder,
        provider_owner=owner.phases.provider_owner,
        structured_output=True,
        _outcome=source.capture,
        checkpoint_early_source=source,
    )


@pytest.mark.asyncio
async def test_original_early_source_refuses_substituted_receivers_before_actual_prepared_call(operation_store, tmp_path, monkeypatch):
    async with (
        _early_case(operation_store, tmp_path, monkeypatch) as (store, owner, _running, selected, primary_sdk),
        _early_attempt(owner, selected) as source,
    ):
        require_original_checkpoint_early_physical_source(source)
        runtime = source.phase_owner.runtime
        advisor = owner.service._advisor_checkpoint
        kwargs = {
            "recorder": runtime.live.recorder,
            "provider_owner": owner.provider_owner,
            "structured_output": True,
            "_outcome": source.capture,
            "checkpoint_early_source": source,
        }
        requests = []
        prepared_inputs = []
        actual_bind = physical_boundary.bind_checkpoint_early_physical_request

        async def checked_bind(actual_source, *, custody, prepared_input, request_options):
            assert actual_source is source and type(prepared_input) is PreparedProviderInput
            assert prepared_input.role == "advisor" and prepared_input.mode == "required"
            assert current_v4_preparation_memo(source.clock) is not None
            prepared_inputs.append(prepared_input)
            before_binding = stored_rows(store[0])
            for changed in (
                {**request_options, "messages": list(request_options["messages"])},
                {**request_options, "model": "foreign/prepared-model"},
            ):
                with pytest.raises(AuditIntegrityError, match="actual prepared invocation"):
                    await actual_bind(actual_source, custody=custody, prepared_input=prepared_input, request_options=changed)
                assert stored_rows(store[0]) == before_binding
            await actual_bind(actual_source, custody=custody, prepared_input=prepared_input, request_options=request_options)
            assert current_v4_preparation_memo(source.clock) is None

        monkeypatch.setattr(physical_boundary, "bind_checkpoint_early_physical_request", checked_bind)
        guidance = json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The approach is coherent.", "note": None})

        async def complete(**request):
            requests.append(request)
            return _reply(guidance)

        monkeypatch.setattr(litellm, "acompletion", complete)
        returned, _metadata = await _invoke_early(source)
        assert returned == guidance and len(requests) == len(prepared_inputs) == 1 and len(primary_sdk) == 1
        assert current_v4_preparation_memo(source.clock) is None
        before = stored_rows(store[0])
        with pytest.raises(AuditIntegrityError, match="original issued observation"):
            require_original_checkpoint_early_physical_source(replace(source))
        for foreign in (
            {"_outcome": _AdvisorInvocationCapture()},
            {"provider_owner": ProviderInvocationOwner(service=store[3], required_work=owner.work)},
            {"recorder": BufferingRecorder()},
            {"checkpoint_early_source": replace(source)},
        ):
            with pytest.raises(AuditIntegrityError, match=r"original capture|original issued observation"):
                await advisor._call_advisor_with_audit(_early_arguments(source), **{**kwargs, **foreign})
            assert stored_rows(store[0]) == before and len(primary_sdk) == len(requests) == 1 and source.capture.claimed
        custody = checkpoint_early_physical_custody(source)
        prepared, admission = custody.continuation_checkpoint_v4, custody.continuation_checkpoint_v4_admission
        assert prepared is not None and admission is not None and custody.attempt is admission.attempt
        assert require_checkpoint_early_prepared_clock(prepared) is source.clock
        detached = checkpoint_early_detached_runtime(source)
        assert detached is not runtime and detached.live.recorder is not runtime.live.recorder
        assert source.capture.audit_call is custody.call and custody.call.status is ComposerLLMCallStatus.SUCCESS
        with locked_session_transaction(store[0], str(store[4])) as conn:
            effect = conn.execute(select(effects).where(effects.c.effect_key == admission.hold.effect_key)).one()
            replay = read_checkpoint_provider_source_on_connection(conn, source=admission.hold, effect=effect, names=runtime.env_ref_names)
        assert replay.phase == "early" and replay.retry_ordinal == 0 and replay.early_input is not None
        assert replay.early_input_ref == prepared.prepared_source.early_input_ref
        assert replay.early_input.messages_sha256 == stable_hash(requests[0]["messages"]) == prepared.request_sha256
        assert replay.early_input.env_ref_names == tuple(sorted(runtime.env_ref_names))
        assert replay.early_input.guard.role == "advisor" and replay.early_input.guard.mode == "required"
        observed = observe_v4_early_checkpoint_returned_response(
            source.capture.snapshot(),
            custody=custody,
            policy=runtime.source.policy,
            attempt=admission.attempt,
            context=runtime.bindings.session_operation_context,
            env_ref_names=runtime.env_ref_names,
        )
        assert type(observed) is CheckpointReturnedResponseObservation and observed.audit_call is custody.call
