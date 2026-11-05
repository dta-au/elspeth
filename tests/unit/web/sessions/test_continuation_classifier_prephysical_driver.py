"""A genuine prephysical classifier convergence crosses the outer worker boundary."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from time import monotonic

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions import continuation_classifier_prephysical_failure as prephysical
from elspeth.web.sessions import continuation_driver, continuation_next_iteration
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.continuation_driver import ContinuationSegmentSettled
from elspeth.web.sessions.continuation_iteration_return import read_iteration_stage_on_connection
from elspeth.web.sessions.continuation_next_iteration import advance_continuation_iteration
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_budgets import OWNER_LEASE_SECONDS
from tests.helpers.continuation_policy import classifier_15s_probe_settings, continuation_test_settings
from tests.unit.web.sessions import test_continuation_classifier_primary as classifier_fixture
from tests.unit.web.sessions import test_continuation_live_phases as phase_fixture
from tests.unit.web.sessions import test_continuation_v4_metadata_live as metadata_fixture
from tests.unit.web.sessions.test_continuation_classifier_primary_v4_paid import _v4_applied_timed_case
from tests.unit.web.sessions.test_continuation_classifier_primary_v4_paid import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import _assert_required_guard_admitted
from tests.unit.web.sessions.test_continuation_tool_failure import _actual_failure_driver

operation_store = _operation_store


@asynccontextmanager
async def _warm_progress_owned_metadata_case(store, tmp_path, monkeypatch):
    """Keep the actual tool/P4 owner and start its driver progress before runtime binding."""
    settings = classifier_15s_probe_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    policy = continuation_policy_v4_from_settings(settings)
    assert policy is not None and policy.actions.primary.preparation == 15.0
    authority = ComposerAsyncOperationAuthority(
        store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    current_store = (store[0], store[1], authority, store[3], store[4])
    monkeypatch.setattr(metadata_fixture.bootstrap_fixture, "minimal_continuation_policy", lambda: policy)
    actual_work = classifier_fixture.initialization.inventory_work

    def timed_work(running):
        stored = authority.load_continuation_snapshot(running)
        root = decode_continuation_snapshot(stored.payload_json)
        admitted = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        sampled = monotonic()
        with store[0].connect() as conn:
            observed = database_now(conn)
            deadline = conn.execute(
                select(segments.c.deadline_at).where(
                    segments.c.session_id == str(running.claim.session_id),
                    segments.c.operation_id == running.claim.operation_id,
                    segments.c.sequence == running.claim.segment_sequence,
                )
            ).scalar_one()
        assert deadline is not None
        clock = ContinuationSQLDeadlineOwner(
            running,
            policy=admitted,
            drain_signal=ComposerDrainSignal(),
            drain_seconds=continuation_test_settings().composer_async_drain_seconds,
            hard_expires_at=sampled + (restore_utc(deadline) - observed).total_seconds(),
        )
        return actual_work(running, sql_deadline_owner=clock)

    monkeypatch.setattr(classifier_fixture.initialization, "inventory_work", timed_work)
    runtime, work, requests = await phase_fixture.initialized_primary_input(
        current_store,
        tmp_path,
        monkeypatch,
        settings_overrides={
            "composer_max_composition_turns": 1,
            "composer_continuation_max_retained_payload_bytes": policy.max_retained_payload_bytes,
            "composer_continuation_action_bounds": settings.composer_continuation_action_bounds,
        },
    )
    running = work.running
    assert running is not None and type(runtime.bindings.provider_owner) is ProviderInvocationOwner
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS, required_work=work.coordinator
    ).join()
    owner = ContinuationLivePhases(runtime.materialization.service, authority, work, runtime.bindings.provider_owner, lease)
    clock = work.coordinator.sql_deadline_owner
    assert type(clock) is ContinuationSQLDeadlineOwner
    driver = _actual_failure_driver(current_store, owner, running, drain_signal=clock.drain_signal)
    driver.work, driver.provider = owner.work, owner.provider_owner
    prior_sdk = []
    try:
        assert owner.runtime is None and driver.progress_owner is None
        await driver.start_progress(await driver.record())
        assert driver.progress_owner is not None and owner.progress is driver.progress_owner.sink
        owner.bind_initial(runtime)

        async def completion(**sdk_kwargs):
            _assert_required_guard_admitted(
                store[0],
                running.session_operation_context,
                requests,
                role="composer",
                user_prompt=runtime.bindings.message,
            )
            prior_sdk.append(sdk_kwargs)
            return ModelResponse(
                model="test/model",
                choices=[
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "Inspecting the existing state.",
                            "tool_calls": [
                                {
                                    "id": "original_v4_tool",
                                    "type": "function",
                                    "function": {
                                        "name": "set_metadata",
                                        "arguments": canonical_json(
                                            encode_semantic_arguments(
                                                "set_metadata",
                                                owner.service._planner_dialect,
                                                {"patch": {"name": "V4 original model metadata"}},
                                            )
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
            )

        monkeypatch.setattr(litellm, "acompletion", completion)
        _messages, _tools, delivery, _delivery_input = owner.service._prepare_primary_delivery(
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            turn_evidence=runtime.live.turn_evidence,
            state=runtime.live.state,
            failed_turn=runtime.live.failed_turn,
        )
        with prompt_guard_user_scope(runtime.bindings.message):
            primary = await run_continuation_primary(
                service=owner.service,
                authority=authority,
                selected=runtime.source.selected,
                producers=runtime.materialization,
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                drained=runtime.drained,
                required_work=work,
                provider_owner=owner.provider_owner,
                timeout=runtime.source.policy.actions.primary.total,
            )
        if delivery is not None:
            runtime.live.turn_evidence.confirm_delivery(delivery, runtime.live.recorder.llm_calls)
        owner.accept_primary(primary)
        selected = await load_selected_continuation(authority=authority, running=running, selected=primary.snapshot, required_work=work)
        await owner.service.advance_continuation_phase(
            owner=owner, authority=authority, selected=selected, work=work, provider_owner=owner.provider_owner
        )
        assert owner.runtime is not None and owner.runtime.live is runtime.live and len(prior_sdk) == 1
        yield current_store, owner, running, driver, prior_sdk
    finally:
        await driver.close_progress()
        await lease.close()


@pytest.mark.asyncio
async def test_actual_worker_retains_prephysical_classifier_convergence_without_sdk_or_retry(operation_store, tmp_path, monkeypatch):
    original_input = phase_fixture.initialized_primary_input

    async def capped_input(current_store, current_path, current_patch, *, settings_overrides=None):
        return await original_input(
            current_store,
            current_path,
            current_patch,
            settings_overrides={**(settings_overrides or {}), "composer_max_composition_turns": 1},
        )

    monkeypatch.setattr(phase_fixture, "initialized_primary_input", capped_input)
    async with _v4_applied_timed_case(operation_store, tmp_path, monkeypatch) as (
        fold_owner,
        running,
        applied,
        _inventory,
        _captured,
        prior_sdk,
    ):
        store = (operation_store[0], operation_store[1], fold_owner.authority, operation_store[3], operation_store[4])
        clock = fold_owner.work.coordinator.sql_deadline_owner
        assert type(clock) is ContinuationSQLDeadlineOwner
        fresh = ContinuationLivePhases(
            fold_owner.service, fold_owner.authority, fold_owner.work, fold_owner.provider_owner, fold_owner.lease
        )
        assert fresh.runtime is None and fold_owner.service._max_composition_turns == 1
        driver = _actual_failure_driver(store, fresh, running, drain_signal=clock.drain_signal)
        driver.work, driver.provider = fresh.work, fresh.provider_owner
        assert await advance_continuation_iteration(fold_owner, driver.services, applied) is None
        ready = fold_owner.authority.load_continuation_snapshot(running)
        with store[0].connect() as conn:
            stage = read_iteration_stage_on_connection(conn, running, selected=ready)
        assert stage is not None and stage.stage == "classify_ready"
        assert ready.cursor == applied.stack.cursor + 1 and fresh.runtime is None
        sid, oid = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            before_physical = tuple(conn.execute(select(physical).where(physical.c.session_id == sid)))
            before_holds = tuple(conn.execute(select(holds).where(holds.c.session_id == sid)))
            before_ledger = tuple(conn.execute(select(ledger).where(ledger.c.session_id == sid)))
        originals = []
        sdk_calls = []
        actual_observer = continuation_next_iteration.observe_continuation_iteration_classification

        async def expire_then_observe(phases, runtime, *, selected, dispatch, persist):
            assert phases is fresh and phases.action_clock is not None
            # Spend this action's original preparation allowance. The real
            # observer/issuer must produce the error and source marker.
            await asyncio.sleep(max(0.0, phases.action_clock.preparation_deadline_monotonic - monotonic()) + 0.001)
            try:
                return await actual_observer(phases, runtime, selected=selected, dispatch=dispatch, persist=persist)
            except ComposerConvergenceError as original:
                marker = prephysical.original_classifier_prephysical_failure(phases, original)
                assert type(marker) is prephysical.ClassifierPrephysicalFailure
                assert type(original.__context__) is TimeoutError
                assert clock.remaining_action(monotonic_now=monotonic()) > 0
                originals.append((original, marker))
                raise

        async def forbidden_sdk(**kwargs):
            sdk_calls.append(kwargs)
            raise AssertionError("prephysical classifier entered another SDK")

        key = running.claim.session_id, oid
        local_cancel = asyncio.Event()
        assert key not in driver.worker._local_cancels
        driver.worker._local_cancels[key] = local_cancel
        actual_verify = type(driver.worker)._verify_terminal_before_release
        verified_reads = []

        async def verify(self, *args, **kwargs):
            result = await actual_verify(self, *args, **kwargs)
            verified_reads.append(result)
            return result

        def construct(*args, **kwargs):
            assert args == (driver.worker, driver.services, running, fold_owner.lease) and not kwargs
            return driver

        monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(continuation_next_iteration, "observe_continuation_iteration_classification", expire_then_observe)
                scoped.setattr(continuation_driver, "_SegmentDriver", construct)
                scoped.setattr(type(driver.worker), "_verify_terminal_before_release", verify)
                outcome = await continuation_driver.run_continuation_segment(driver.worker, driver.services, running, fold_owner.lease)
        finally:
            assert driver.worker._local_cancels.pop(key) is local_cancel

        assert len(originals) == len(verified_reads) == 1 and not sdk_calls and len(prior_sdk) == 1
        assert type(outcome) is ContinuationSegmentSettled
        terminal = outcome.terminal
        assert terminal.status == "failed" and terminal.failure_code == "http_error"
        assert terminal.result_json is not None
        public = json.loads(terminal.result_json)
        detail = public["body"]["detail"]
        assert public["http_status"] == 422 and public["error_type"] == detail["error_type"] == "convergence"
        assert detail["budget_exhausted"] == "timeout" and detail["turns_used"] == 1
        assert "timeout_seconds" not in detail and detail["partial_state"]["metadata"]["name"] == "V4 original model metadata"
        with store[0].connect() as conn:
            verified = prephysical.read_classifier_prephysical_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=oid
            )
            assert verified is not None and verified.terminal == terminal
            assert tuple(conn.execute(select(physical).where(physical.c.session_id == sid))) == before_physical
            assert tuple(conn.execute(select(holds).where(holds.c.session_id == sid))) == before_holds
            assert tuple(conn.execute(select(ledger).where(ledger.c.session_id == sid))) == before_ledger
            assert tuple(
                conn.execute(
                    select(events.c.tag)
                    .where(events.c.session_id == sid, events.c.operation_id == oid, events.c.sequence > verified.original_source.cursor)
                    .order_by(events.c.sequence)
                ).scalars()
            ) == ("effect_unknown", "settled")
            effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.effect_key == verified.effect_key)).one()
            parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == oid)).one()
            fence = conn.execute(
                select(fences).where(
                    fences.c.session_id == sid,
                    fences.c.operation_id == running.session_operation_context.fence.operation_id,
                )
            ).one()
            assert effect.disposition == "unknown" and effect.provider_attempt_id is None
            assert parent.status == "failed" and parent.result_json == terminal.result_json
            assert fence.released_at is not None
        assert fold_owner.lease.closed and fold_owner.lease._close_task is not None and fold_owner.lease._close_task.done()
        assert fold_owner.lease._renewal_task is not None and fold_owner.lease._renewal_task.done()
        fold_owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_actual_warm_tool_owner_keeps_successful_custody_out_of_classifier_failure(operation_store, tmp_path, monkeypatch):
    async with _warm_progress_owned_metadata_case(operation_store, tmp_path, monkeypatch) as (store, warm, running, driver, prior_sdk):
        stored = warm.latest_snapshot
        assert stored is not None and warm.finished_batch is None
        selected = await load_selected_continuation(authority=warm.authority, running=running, selected=stored, required_work=warm.work)
        custody = warm.failure_custody
        assert custody is not None and not custody.completed
        assert custody.original is None and custody.errors == [] and custody.wrapper is None
        assert warm.service._max_composition_turns == 1 and len(prior_sdk) == 1
        clock = warm.work.coordinator.sql_deadline_owner
        assert type(clock) is ContinuationSQLDeadlineOwner
        assert driver.phases is warm and driver.work is warm.work and driver.provider is warm.provider_owner
        assert driver.progress_owner is not None and warm.progress is driver.progress_owner.sink
        assert await advance_continuation_iteration(warm, driver.services, selected) is None
        ready = warm.authority.load_continuation_snapshot(running)
        with store[0].connect() as conn:
            stage = read_iteration_stage_on_connection(conn, running, selected=ready)
        assert stage is not None and stage.stage == "classify_ready"
        assert ready.cursor == selected.stack.cursor + 1 and warm.failure_custody is custody
        sid, oid = str(running.claim.session_id), running.claim.operation_id
        with store[0].connect() as conn:
            before_physical = tuple(conn.execute(select(physical).where(physical.c.session_id == sid)))
            before_holds = tuple(conn.execute(select(holds).where(holds.c.session_id == sid)))
            before_ledger = tuple(conn.execute(select(ledger).where(ledger.c.session_id == sid)))
            before_tools = tuple(
                conn.execute(
                    select(effects.c.effect_key, effects.c.disposition)
                    .where(effects.c.session_id == sid, effects.c.owner == "tool_dispatch")
                    .order_by(effects.c.effect_key)
                )
            )
        originals = []
        sdk_calls = []
        actual_observer = continuation_next_iteration.observe_continuation_iteration_classification

        async def expire_then_observe(phases, runtime, *, selected, dispatch, persist):
            assert phases is warm and phases.action_clock is not None
            assert phases.failure_custody is custody and not custody.completed and custody.original is None and custody.errors == []
            await asyncio.sleep(max(0.0, phases.action_clock.preparation_deadline_monotonic - monotonic()) + 0.001)
            try:
                return await actual_observer(phases, runtime, selected=selected, dispatch=dispatch, persist=persist)
            except ComposerConvergenceError as original:
                marker = prephysical.original_classifier_prephysical_failure(phases, original)
                assert type(marker) is prephysical.ClassifierPrephysicalFailure
                assert type(original.__context__) is TimeoutError
                assert clock.remaining_action(monotonic_now=monotonic()) > 0
                originals.append((original, marker))
                raise

        async def forbidden_sdk(**kwargs):
            sdk_calls.append(kwargs)
            raise AssertionError("warm prephysical classifier entered another SDK")

        key = running.claim.session_id, oid
        local_cancel = asyncio.Event()
        assert key not in driver.worker._local_cancels
        driver.worker._local_cancels[key] = local_cancel

        def construct(*args, **kwargs):
            assert args == (driver.worker, driver.services, running, warm.lease) and not kwargs
            return driver

        monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(continuation_next_iteration, "observe_continuation_iteration_classification", expire_then_observe)
                scoped.setattr(continuation_driver, "_SegmentDriver", construct)
                outcome = await continuation_driver.run_continuation_segment(driver.worker, driver.services, running, warm.lease)
        finally:
            assert driver.worker._local_cancels.pop(key) is local_cancel

        assert len(originals) == 1 and not sdk_calls and len(prior_sdk) == 1
        assert warm.failure_custody is custody and not custody.completed
        assert custody.original is None and custody.errors == [] and custody.wrapper is None
        assert type(outcome) is ContinuationSegmentSettled
        terminal = outcome.terminal
        assert terminal.status == "failed" and terminal.failure_code == "http_error" and terminal.result_json is not None
        public = json.loads(terminal.result_json)
        detail = public["body"]["detail"]
        assert public["http_status"] == 422 and public["error_type"] == detail["error_type"] == "convergence"
        assert detail["budget_exhausted"] == "timeout" and detail["turns_used"] == 1
        assert "timeout_seconds" not in detail
        with store[0].connect() as conn:
            verified = prephysical.read_classifier_prephysical_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=oid
            )
            assert verified is not None and verified.terminal == terminal
            assert tuple(conn.execute(select(physical).where(physical.c.session_id == sid))) == before_physical
            assert tuple(conn.execute(select(holds).where(holds.c.session_id == sid))) == before_holds
            assert tuple(conn.execute(select(ledger).where(ledger.c.session_id == sid))) == before_ledger
            assert (
                tuple(
                    conn.execute(
                        select(effects.c.effect_key, effects.c.disposition)
                        .where(effects.c.session_id == sid, effects.c.owner == "tool_dispatch")
                        .order_by(effects.c.effect_key)
                    )
                )
                == before_tools
            )
            assert tuple(
                conn.execute(
                    select(events.c.tag)
                    .where(events.c.session_id == sid, events.c.operation_id == oid, events.c.sequence > verified.original_source.cursor)
                    .order_by(events.c.sequence)
                ).scalars()
            ) == ("effect_unknown", "settled")
            fence = conn.execute(
                select(fences).where(
                    fences.c.session_id == sid,
                    fences.c.operation_id == running.session_operation_context.fence.operation_id,
                )
            ).one()
            assert fence.released_at is not None
        assert warm.lease.closed and warm.lease._close_task is not None and warm.lease._close_task.done()
        assert warm.lease._renewal_task is not None and warm.lease._renewal_task.done()
        warm.work.coordinator.assert_completed()
