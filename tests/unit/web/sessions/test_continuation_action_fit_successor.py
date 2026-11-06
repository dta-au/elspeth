"""A later Compose action-fit yield creates a cold successor without replay."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from time import perf_counter
from traceback import extract_tb
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from elspeth.web.composer import continuation_semantic_advisor_phase as semantic_phase
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.continuation_settings import ContinuationActionBoundsSettings
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions import continuation_semantic_actions as semantic_actions
from elspeth.web.sessions.continuation import load_snapshot, read_chunk
from elspeth.web.sessions.continuation_driver import _SegmentDriver
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ScriptedSDK,
    continuation_app,
    effects,
    end_admit,
    install_sdk,
    observe_original_driver_failures,
    semantic_primary_response,
    semantic_read_response,
    semantic_review_response,
    semantic_stop_response,
    submit_and_settle,
)
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.sessions.test_continuation_control_successor import _control


def _advisor_preparation_probe_bounds() -> ContinuationActionBoundsSettings:
    """Give this multi-owner live probe measured room without changing defaults."""
    original = continuation_test_settings().composer_continuation_action_bounds
    return ContinuationActionBoundsSettings.model_validate(
        {**original.model_dump(), "advisor": {**original.advisor.model_dump(), "preparation": 15.0}}
    )


@pytest.mark.asyncio
async def test_advisor_preparation_probe_keeps_the_original_expiry() -> None:
    original = continuation_policy_from_settings(continuation_test_settings()).actions.advisor
    widened = _advisor_preparation_probe_bounds().advisor
    assert original.preparation == 5.0 and widened.preparation == 15.0
    clock = ContinuationActionClock(original)
    with pytest.raises(TimeoutError, match="preparation allowance expired"):
        clock.require_preparation_time(monotonic_now=clock.preparation_deadline_monotonic + 0.001)


def _observe_owner_errors(monkeypatch, operation_id):
    trace = []
    original_run = _SegmentDriver.run
    original_yield = _SegmentDriver.yield_control

    async def observe_run(driver, *args, **kwargs):
        if driver.running.claim.operation_id != operation_id:
            return await original_run(driver, *args, **kwargs)
        try:
            outcome = await original_run(driver, *args, **kwargs)
        except BaseException as error:
            trace.append(
                (
                    "run-error",
                    driver.running.claim.segment_sequence,
                    type(error).__name__,
                    tuple((frame.name, frame.lineno) for frame in extract_tb(error.__traceback__)[-8:]),
                )
            )
            raise
        trace.append(("run-return", driver.running.claim.segment_sequence, type(outcome).__name__))
        return outcome

    async def observe_yield(driver, *args, **kwargs):
        if driver.running.claim.operation_id != operation_id:
            return await original_yield(driver, *args, **kwargs)
        try:
            outcome = await original_yield(driver, *args, **kwargs)
        except BaseException as error:
            trace.append(
                (
                    "yield-error",
                    driver.running.claim.segment_sequence,
                    kwargs.get("reason"),
                    type(error).__name__,
                    tuple((frame.name, frame.lineno) for frame in extract_tb(error.__traceback__)[-8:]),
                )
            )
            raise
        trace.append(("yield-return", driver.running.claim.segment_sequence, kwargs.get("reason"), type(outcome).__name__))
        return outcome

    monkeypatch.setattr(_SegmentDriver, "run", observe_run)
    monkeypatch.setattr(_SegmentDriver, "yield_control", observe_yield)
    return trace


def _profile_review_proofs(monkeypatch):
    """Bounded first-cut diagnostic: elapsed time and SQL count, no SQL text."""
    profile = []

    def instrument(name, original):
        def observed(conn, *args, **kwargs):
            sql_count = 0

            def count_sql(_conn, _cursor, _statement, _parameters, _context, _executemany):
                nonlocal sql_count
                sql_count += 1

            event.listen(conn, "before_cursor_execute", count_sql)
            start = perf_counter()
            try:
                return original(conn, *args, **kwargs)
            finally:
                elapsed = perf_counter() - start
                event.remove(conn, "before_cursor_execute", count_sql)
                profile.append((name, round(elapsed, 4), sql_count))

        return observed

    for module, name, original in (
        (semantic_actions, "_reprove_installed_snapshot", semantic_actions._reprove_installed_snapshot),
        (semantic_actions, "_consumed_review_from_snapshot", semantic_actions._consumed_review_from_snapshot),
        (
            semantic_actions,
            "require_installed_semantic_action_on_connection",
            semantic_actions.require_installed_semantic_action_on_connection,
        ),
        (
            semantic_actions,
            "require_consumed_semantic_review_on_connection",
            semantic_actions.require_consumed_semantic_review_on_connection,
        ),
        (semantic_phase, "consume_semantic_advisor_review_on_connection", semantic_phase.consume_semantic_advisor_review_on_connection),
    ):
        monkeypatch.setattr(module, name, instrument(name, original))
    return profile


def _failure_state(harness, operation_id, sdk, timing, driver_trace, owner_trace, proof_profile=None):
    with harness.app.state.session_engine.connect() as conn:
        parent = conn.execute(
            select(jobs.c.status, jobs.c.phase, jobs.c.current_segment_sequence, jobs.c.ownership_generation).where(
                jobs.c.operation_id == operation_id
            )
        ).one_or_none()
        segment_summary = conn.execute(
            select(segments.c.sequence, segments.c.generation, segments.c.state, segments.c.rollover_reason)
            .where(segments.c.operation_id == operation_id)
            .order_by(segments.c.sequence)
        ).all()
        event_summary = conn.execute(
            select(events.c.sequence, events.c.tag).where(events.c.operation_id == operation_id).order_by(events.c.sequence)
        ).all()
        latest = load_snapshot(conn, session_id=str(harness.session_id), operation_id=operation_id)
        top = decode_continuation_snapshot(latest.payload_json).frames[-1]
    return {
        "parent": parent,
        "segments": segment_summary,
        "events": event_summary,
        "top": (type(top).__name__, top.phase if type(top) is ComposeFrame else top.frame_kind),
        "sdk_roles": sdk.roles,
        "timing": timing,
        "owner": owner_trace[-10:],
        "proof_profile": [] if proof_profile is None else proof_profile[-15:],
        "driver": driver_trace[-10:],
    }


async def _settle_with_bounded_failure_state(
    harness, body, sdk, timing, driver_trace, owner_trace, proof_profile=None, *, expected_status="completed", rounds=200
):
    try:
        terminal = await submit_and_settle(harness, body, rounds=rounds)
    except AssertionError as original:
        if str(original) == "Continuation request did not settle":
            original.add_note(
                f"bounded owner state: {_failure_state(harness, body['operation_id'], sdk, timing, driver_trace, owner_trace, proof_profile)!r}"
            )
        raise
    if terminal.status != expected_status:
        pytest.fail(
            f"continuation terminal failed: {(terminal.status, terminal.error.failure_code if terminal.error else None)!r}; "
            f"{_failure_state(harness, body['operation_id'], sdk, timing, driver_trace, owner_trace, proof_profile)!r}",
            pytrace=False,
        )
    return terminal


def _stop_after_committed_primary(monkeypatch, harness, operation_id, *, sequence: int, generation_at_least: int, count: int):
    """Request the real durable Stop only after the selected PRIMARY is committed."""
    observed = []
    original_advance = _SegmentDriver.advance

    async def advance_then_stop(driver, stored, selected):
        result = await original_advance(driver, stored, selected)
        if (
            driver.running.claim.operation_id != operation_id
            or driver.running.claim.segment_sequence != sequence
            or driver.running.claim.ownership_generation < generation_at_least
            or observed
            or type(selected.stack.frames[-1]) is not ComposeFrame
            or selected.stack.frames[-1].phase != "action_ready"
        ):
            return result
        primary = tuple(row for row in effects(harness.app, operation_id) if row.effect_key.startswith("provider:0:"))
        if len(primary) != count or any(row.disposition != "committed" for row in primary):
            return result
        stopped = harness.app.state.composer_async_operation_authority.request_cancel(
            session_id=harness.session_id, operation_id=operation_id
        )
        assert stopped is not None and stopped.cancel_requested_at is not None
        observed.append((driver.running.claim.segment_sequence, driver.running.claim.ownership_generation, primary))
        return result

    monkeypatch.setattr(_SegmentDriver, "advance", advance_then_stop)
    return observed


@pytest.mark.asyncio
async def test_later_compose_action_fit_yields_after_model_control_without_replaying_provider(tmp_path, monkeypatch):
    sdk = ScriptedSDK(
        [
            ("primary", ("Continue this same request at the retained checkpoint.", [_control()])),
            ("semantic", semantic_primary_response),
            ("primary", ("The pipeline explanation is ready for review.", None)),
            ("semantic", semantic_review_response),
            ("end", end_admit()),
            ("semantic", semantic_stop_response),
        ]
    )
    install_sdk(monkeypatch, sdk)
    operation_id = str(uuid4())
    async with continuation_app(tmp_path, monkeypatch, composer_continuation_action_bounds=_advisor_preparation_probe_bounds()) as harness:
        sdk.app_ref.append(harness.app)
        driver_trace = observe_original_driver_failures(monkeypatch, operation_id, session_id=harness.session_id)
        owner_trace = _observe_owner_errors(monkeypatch, operation_id)
        actual_pending_control = _SegmentDriver.pending_control

        async def observe_pending_control(driver, selected):
            if driver.running.claim.operation_id == operation_id and driver.running.claim.segment_sequence > 1:
                assert driver.progress_owner is not None
                assert driver.phases.progress is driver.progress_owner.sink
            return await actual_pending_control(driver, selected)

        monkeypatch.setattr(_SegmentDriver, "pending_control", observe_pending_control)
        actual_timing = ComposerAsyncOperationAuthority.read_continuation_timing
        observed_fit = []

        def observe_action_fit(owner, running, *, deadline=None):
            timing = actual_timing(owner, running, deadline=deadline)
            if running.claim.operation_id != operation_id or running.claim.segment_sequence != 2 or observed_fit:
                return timing
            with harness.app.state.session_engine.connect() as conn:
                selected = load_snapshot(conn, session_id=str(harness.session_id), operation_id=operation_id)
                top = decode_continuation_snapshot(selected.payload_json).frames[-1]
            if type(top) is not ComposeFrame or top.phase != "action_ready" or sdk.roles != ["primary"]:
                return timing
            observed_fit.append((timing.sequence, timing.generation, selected.checkpoint_id))
            # The test controls only the returned timing observation. The
            # transaction still checks the real current fence and SQL head.
            return replace(timing, hard_remaining_seconds=0.0, cooperative_remaining_seconds=0.0, yield_pending=True)

        monkeypatch.setattr(ComposerAsyncOperationAuthority, "read_continuation_timing", observe_action_fit)
        await _settle_with_bounded_failure_state(
            harness, message_body("Explain this pipeline.", operation_id=operation_id), sdk, observed_fit, driver_trace, owner_trace
        )
        assert observed_fit and sdk.roles == ["primary", "semantic", "primary", "semantic", "end", "semantic"] and not sdk.steps
        with harness.app.state.session_engine.connect() as conn:
            control, predecessor, successor = conn.execute(
                select(segments).where(segments.c.operation_id == operation_id).order_by(segments.c.sequence)
            ).all()
            assert (control.sequence, control.state, control.rollover_reason) == (1, "yielded", "model_request")
            assert (predecessor.sequence, predecessor.state, predecessor.rollover_reason) == (2, "yielded", "action_fit")
            assert (successor.sequence, successor.predecessor_sequence, successor.state) == (3, 2, "settled")
            assert successor.generation == predecessor.generation + 1
            yielded_events = conn.execute(
                select(events).where(events.c.operation_id == operation_id, events.c.tag == "yielded").order_by(events.c.sequence)
            ).all()
            assert len(yielded_events) == 2
            yielded_event = yielded_events[1]
            yielded_checkpoint = conn.execute(
                select(checkpoints).where(checkpoints.c.operation_id == operation_id, checkpoints.c.cursor == yielded_event.sequence)
            ).one()
            root = decode_continuation_snapshot(
                read_chunk(
                    conn,
                    session_id=str(harness.session_id),
                    operation_id=operation_id,
                    key=yielded_checkpoint.snapshot_chunk_key,
                ).decode("utf-8")
            )
        assert len(root.frames) == 1 and type(root.frames[0]) is ComposeFrame
        assert root.frames[0].slice_sequence == 3
        assert root.frames[0].composition_slice == root.frames[0].discovery_slice == 0
        primary = tuple(row for row in effects(harness.app, operation_id) if row.effect_key.startswith("provider:0:"))
        assert len(primary) == 2
        assert [row.creator_sequence for row in primary] == [1, 3]
        assert all(row.disposition == "committed" for row in primary)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["quantum", "watermark"])
async def test_original_elapsed_cooperative_boundary_yields_a_cold_successor(tmp_path, monkeypatch, reason):
    settings = {
        "composer_continuation_action_bounds": _advisor_preparation_probe_bounds(),
        "composer_continuation_quantum_seconds": 60.0 if reason == "quantum" else 3600.0,
        "composer_continuation_watermark_seconds": 120.0 if reason == "quantum" else 360.0,
    }
    # These settings are validated before admission. The actual clocks and SQL
    # timestamps remain untouched; this is not a default-latency qualification.
    policy = continuation_policy_from_settings(continuation_test_settings(**settings))
    assert policy is not None
    required = policy.actions.advisor.total + policy.segment.reserve
    if reason == "watermark":
        assert required < policy.segment.watermark
    sdk = ScriptedSDK(
        [
            ("primary", ("Continue this original request at its retained checkpoint.", [_control()])),
            ("semantic", semantic_primary_response),
            ("primary", ("The cold successor retains one new original PRIMARY.", None)),
        ]
    )
    operation_id = str(uuid4())
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch, **settings) as harness:
        sdk.app_ref.append(harness.app)
        driver_trace = observe_original_driver_failures(monkeypatch, operation_id, session_id=harness.session_id)
        owner_trace = _observe_owner_errors(monkeypatch, operation_id)
        stopped = _stop_after_committed_primary(monkeypatch, harness, operation_id, sequence=3, generation_at_least=3, count=2)
        original_timing = ComposerAsyncOperationAuthority.read_continuation_timing
        elapsed_observations, before_roots = [], []

        def wait_for_original_boundary(owner, running, *, deadline=None):
            observed = original_timing(owner, running, deadline=deadline)
            if running.claim.operation_id != operation_id or running.claim.segment_sequence != 2 or elapsed_observations:
                return observed
            assert threading.current_thread() is not threading.main_thread()
            with harness.app.state.session_engine.connect() as conn:
                selected = load_snapshot(conn, session_id=str(harness.session_id), operation_id=operation_id)
                root = decode_continuation_snapshot(selected.payload_json)
            assert len(root.frames) == 1 and type(root.frames[0]) is ComposeFrame and root.frames[0].phase == "action_ready"
            assert sdk.roles == ["primary"] and observed.cooperative_remaining_seconds > 0
            before = time.monotonic()
            # This is the real registered timing worker. Wait without holding a
            # transaction, then return another original SQL timing observation.
            time.sleep(observed.cooperative_remaining_seconds + 0.05)
            actual = original_timing(owner, running, deadline=deadline)
            elapsed = time.monotonic() - before
            assert actual.cooperative_remaining_seconds == 0.0
            assert actual.hard_remaining_seconds > required
            assert (actual.hard_remaining_seconds <= policy.segment.watermark) is (reason == "watermark")
            elapsed_observations.append((observed, actual, elapsed))
            before_roots.append(root)
            return actual

        monkeypatch.setattr(ComposerAsyncOperationAuthority, "read_continuation_timing", wait_for_original_boundary)
        terminal = await _settle_with_bounded_failure_state(
            harness,
            message_body("Explain this pipeline.", operation_id=operation_id),
            sdk,
            elapsed_observations,
            driver_trace,
            owner_trace,
            expected_status="failed",
            # Real elapsed boundaries plus the complete cold successor need a
            # larger observation window, without changing any admitted clock.
            rounds=1440,
        )
        assert terminal.error is not None and terminal.error.error_type == "request_cancelled"
        assert len(elapsed_observations) == len(stopped) == 1
        assert sdk.roles == ["primary", "semantic", "primary"] and not sdk.steps
        with harness.app.state.session_engine.connect() as conn:
            control, predecessor, successor = conn.execute(
                select(segments).where(segments.c.operation_id == operation_id).order_by(segments.c.sequence)
            ).all()
            original_attempts = conn.execute(
                select(attempts).where(attempts.c.operation_id == operation_id).order_by(attempts.c.sequence)
            ).all()
            yielded = conn.execute(
                select(events).where(events.c.operation_id == operation_id, events.c.tag == "yielded").order_by(events.c.sequence)
            ).all()
            assert len(yielded) == 2
            checkpoint = conn.execute(
                select(checkpoints).where(checkpoints.c.operation_id == operation_id, checkpoints.c.cursor == yielded[1].sequence)
            ).one()
            successor_root = decode_continuation_snapshot(
                read_chunk(conn, session_id=str(harness.session_id), operation_id=operation_id, key=checkpoint.snapshot_chunk_key).decode()
            )
            user_messages = conn.execute(
                select(messages.c.id).where(messages.c.session_id == str(harness.session_id), messages.c.role == "user")
            ).all()
        assert (control.sequence, control.rollover_reason) == (1, "model_request")
        assert (predecessor.sequence, predecessor.state, predecessor.rollover_reason) == (2, "yielded", reason)
        assert (successor.sequence, successor.predecessor_sequence, successor.state) == (3, 2, "settled")
        assert successor.generation == predecessor.generation + 1
        assert len(user_messages) == 1
        assert len(original_attempts) == 3 and [row.outcome for row in original_attempts[:2]] == ["yielded", "yielded"]
        assert all(row.revoked_at is not None for row in original_attempts)
        (before_root,) = before_roots
        before_compose, after_compose = before_root.frames[0], successor_root.frames[0]
        assert type(before_compose) is ComposeFrame and type(after_compose) is ComposeFrame
        assert (after_compose.composition_total, after_compose.discovery_total, after_compose.advisor_calls) == (
            before_compose.composition_total,
            before_compose.discovery_total,
            before_compose.advisor_calls,
        )
        assert after_compose.slice_sequence == 3 and after_compose.composition_slice == after_compose.discovery_slice == 0
        assert successor_root.logical == before_root.logical
        assert successor_root.admission_policy_json == before_root.admission_policy_json
        assert successor_root.policy_hash == before_root.policy_hash == policy.policy_hash
        assert successor_root.effect_frontier == before_root.effect_frontier
        assert successor_root.provider_cost_policy == before_root.provider_cost_policy
        assert successor_root.semantic_frontier == before_root.semantic_frontier
        assert successor_root.mutation_ceiling == before_root.mutation_ceiling
        primary = tuple(row for row in effects(harness.app, operation_id) if row.effect_key.startswith("provider:0:"))
        assert len(primary) == 2 and [row.creator_sequence for row in primary] == [1, 3]
        assert all(row.disposition == "committed" for row in primary)


@pytest.mark.asyncio
async def test_original_healthy_primary_crosses_cooperative_boundary_then_completes(tmp_path, monkeypatch):
    operation_id = str(uuid4())
    sdk = ScriptedSDK(
        [
            ("primary", ("The pipeline explanation is ready for review.", None)),
            ("semantic", semantic_read_response),
            ("semantic", semantic_review_response),
            ("end", end_admit()),
            ("semantic", semantic_stop_response),
        ]
    )
    original_advance = _SegmentDriver.advance
    drivers, healthy_actions = [], []

    async def observe_original_action(driver, stored, selected):
        top = selected.stack.frames[-1]
        if driver.running.claim.operation_id == operation_id and type(top) is ComposeFrame and top.phase == "action_ready" and not drivers:
            drivers.append(driver)
        return await original_advance(driver, stored, selected)

    monkeypatch.setattr(_SegmentDriver, "advance", observe_original_action)

    async def healthy_provider_crosses_boundary(**kwargs):
        if healthy_actions:
            return await sdk(**kwargs)
        assert not sdk.requests and len(drivers) == 1
        driver = drivers[0]
        clock = driver._action_clock
        assert type(clock) is ContinuationActionClock and clock._active_stage == "physical"
        original_deadline, original_started = clock.deadline, clock.started_monotonic
        started = time.monotonic()
        await asyncio.sleep(61.0)
        assert time.monotonic() - started >= 60.0
        assert driver._action_clock is clock and clock._active_stage == "physical"
        assert clock.deadline == original_deadline and clock.started_monotonic == original_started
        healthy_actions.append(clock)
        return await sdk(**kwargs)

    install_sdk(monkeypatch, healthy_provider_crosses_boundary)
    async with continuation_app(tmp_path, monkeypatch, composer_continuation_quantum_seconds=60.0) as harness:
        sdk.app_ref.append(harness.app)
        driver_trace = observe_original_driver_failures(monkeypatch, operation_id, session_id=harness.session_id)
        owner_trace = _observe_owner_errors(monkeypatch, operation_id)
        terminal = await _settle_with_bounded_failure_state(
            harness,
            message_body("Explain this pipeline.", operation_id=operation_id),
            sdk,
            [],
            driver_trace,
            owner_trace,
            rounds=720,
        )
        assert terminal.status == "completed" and terminal.error is None and terminal.result is not None
        assert len(healthy_actions) == 1 and sdk.roles == ["primary", "semantic", "semantic", "end", "semantic"] and not sdk.steps
        with harness.app.state.session_engine.connect() as conn:
            (segment,) = conn.execute(select(segments).where(segments.c.operation_id == operation_id)).all()
            recoveries = conn.execute(select(events).where(events.c.operation_id == operation_id, events.c.tag == "recovered")).all()
            original_attempts = conn.execute(
                select(attempts).where(attempts.c.operation_id == operation_id).order_by(attempts.c.generation)
            ).all()
            for recovery in recoveries:
                pair = conn.execute(
                    select(checkpoints)
                    .where(checkpoints.c.operation_id == operation_id, checkpoints.c.cursor.in_((recovery.sequence - 1, recovery.sequence)))
                    .order_by(checkpoints.c.cursor)
                ).all()
                assert len(pair) == 2
                before, after = (
                    decode_continuation_snapshot(
                        read_chunk(conn, session_id=str(harness.session_id), operation_id=operation_id, key=row.snapshot_chunk_key).decode()
                    )
                    for row in pair
                )
                assert after == before.model_copy(update={"cursor": after.cursor})
        assert (segment.sequence, segment.state) == (1, "settled") and segment.generation >= 2
        assert recoveries and len(original_attempts) == segment.generation
        assert all(row.outcome == "interrupted" and row.revoked_at is not None for row in original_attempts[:-1])
        primary = tuple(row for row in effects(harness.app, operation_id) if row.effect_key.startswith("provider:0:"))
        assert len(primary) == 1 and primary[0].disposition == "committed" and primary[0].creator_sequence == 1


@pytest.mark.asyncio
async def test_unconsumed_first_compose_action_fit_renews_same_sequence_without_replaying_provider(tmp_path, monkeypatch):
    sdk = ScriptedSDK(
        [
            ("primary", ("The pipeline explanation is ready for review.", None)),
            ("semantic", semantic_read_response),
            ("semantic", semantic_review_response),
            ("end", end_admit()),
            ("semantic", semantic_stop_response),
        ]
    )
    install_sdk(monkeypatch, sdk)
    operation_id = str(uuid4())
    async with continuation_app(tmp_path, monkeypatch, composer_continuation_action_bounds=_advisor_preparation_probe_bounds()) as harness:
        sdk.app_ref.append(harness.app)
        driver_trace = observe_original_driver_failures(monkeypatch, operation_id, session_id=harness.session_id)
        owner_trace = _observe_owner_errors(monkeypatch, operation_id)
        proof_profile = _profile_review_proofs(monkeypatch)
        actual_timing = ComposerAsyncOperationAuthority.read_continuation_timing
        observed_first = []

        def observe_first_action_fit(owner, running, *, deadline=None):
            timing = actual_timing(owner, running, deadline=deadline)
            if running.claim.operation_id != operation_id or running.claim.segment_sequence != 1 or observed_first:
                return timing
            with harness.app.state.session_engine.connect() as conn:
                selected = load_snapshot(conn, session_id=str(harness.session_id), operation_id=operation_id)
                top = decode_continuation_snapshot(selected.payload_json).frames[-1]
            if type(top) is not ComposeFrame or top.phase != "action_ready":
                return timing
            observed_first.append((timing.sequence, timing.generation, selected.checkpoint_id))
            return replace(timing, hard_remaining_seconds=0.0, cooperative_remaining_seconds=0.0, yield_pending=True)

        monkeypatch.setattr(ComposerAsyncOperationAuthority, "read_continuation_timing", observe_first_action_fit)
        await _settle_with_bounded_failure_state(
            harness,
            message_body("Explain this pipeline.", operation_id=operation_id),
            sdk,
            observed_first,
            driver_trace,
            owner_trace,
            proof_profile,
        )
        assert observed_first and sdk.roles == ["primary", "semantic", "semantic", "end", "semantic"] and not sdk.steps
        with harness.app.state.session_engine.connect() as conn:
            (segment,) = conn.execute(select(segments).where(segments.c.operation_id == operation_id)).all()
            tags = tuple(row.tag for row in conn.execute(select(events.c.tag).where(events.c.operation_id == operation_id)))
        assert (segment.sequence, segment.state) == (1, "settled") and segment.generation >= 2
        assert "recovered" in tags and "yielded" not in tags
        primary = tuple(row for row in effects(harness.app, operation_id) if row.effect_key.startswith("provider:0:"))
        assert len(primary) == 1 and primary[0].creator_sequence == 1 and primary[0].disposition == "committed"


@pytest.mark.asyncio
async def test_later_compose_action_fit_successor_retains_primary_before_durable_stop(tmp_path, monkeypatch):
    sdk = ScriptedSDK(
        [
            ("primary", ("Continue this same request at the retained checkpoint.", [_control()])),
            ("semantic", semantic_primary_response),
            ("primary", ("The retained request continues with one new PRIMARY.", None)),
        ]
    )
    install_sdk(monkeypatch, sdk)
    operation_id = str(uuid4())
    async with continuation_app(tmp_path, monkeypatch, composer_continuation_action_bounds=_advisor_preparation_probe_bounds()) as harness:
        sdk.app_ref.append(harness.app)
        driver_trace = observe_original_driver_failures(monkeypatch, operation_id, session_id=harness.session_id)
        owner_trace = _observe_owner_errors(monkeypatch, operation_id)
        stopped_after_primary = _stop_after_committed_primary(
            monkeypatch, harness, operation_id, sequence=3, generation_at_least=3, count=2
        )
        actual_timing = ComposerAsyncOperationAuthority.read_continuation_timing
        observed_fit = []

        def observe_action_fit(owner, running, *, deadline=None):
            timing = actual_timing(owner, running, deadline=deadline)
            if running.claim.operation_id != operation_id or running.claim.segment_sequence != 2 or observed_fit:
                return timing
            with harness.app.state.session_engine.connect() as conn:
                selected = load_snapshot(conn, session_id=str(harness.session_id), operation_id=operation_id)
                top = decode_continuation_snapshot(selected.payload_json).frames[-1]
            if type(top) is not ComposeFrame or top.phase != "action_ready" or sdk.roles != ["primary"]:
                return timing
            observed_fit.append((timing.sequence, timing.generation, selected.checkpoint_id))
            return replace(timing, hard_remaining_seconds=0.0, cooperative_remaining_seconds=0.0, yield_pending=True)

        monkeypatch.setattr(ComposerAsyncOperationAuthority, "read_continuation_timing", observe_action_fit)
        terminal = await _settle_with_bounded_failure_state(
            harness,
            message_body("Explain this pipeline.", operation_id=operation_id),
            sdk,
            observed_fit,
            driver_trace,
            owner_trace,
            expected_status="failed",
        )
        assert terminal.error is not None and terminal.error.error_type == "request_cancelled"
        assert observed_fit and len(stopped_after_primary) == 1
        assert sdk.roles == ["primary", "semantic", "primary"] and not sdk.steps
        with harness.app.state.session_engine.connect() as conn:
            control, predecessor, successor = conn.execute(
                select(segments).where(segments.c.operation_id == operation_id).order_by(segments.c.sequence)
            ).all()
            yielded = conn.execute(select(events).where(events.c.operation_id == operation_id, events.c.tag == "yielded")).all()
        assert (control.sequence, control.state, control.rollover_reason) == (1, "yielded", "model_request")
        assert (predecessor.sequence, predecessor.state, predecessor.rollover_reason) == (2, "yielded", "action_fit")
        assert (successor.sequence, successor.predecessor_sequence, successor.state) == (3, 2, "settled")
        assert successor.generation == predecessor.generation + 1 and len(yielded) == 2
        primary = tuple(row for row in effects(harness.app, operation_id) if row.effect_key.startswith("provider:0:"))
        assert len(primary) == 2 and [row.creator_sequence for row in primary] == [1, 3]
        assert all(row.disposition == "committed" for row in primary)


@pytest.mark.asyncio
async def test_first_compose_action_fit_recovers_once_before_durable_stop(tmp_path, monkeypatch):
    sdk = ScriptedSDK([("primary", ("The original Compose produces one retained PRIMARY.", None))])
    install_sdk(monkeypatch, sdk)
    operation_id = str(uuid4())
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sdk.app_ref.append(harness.app)
        driver_trace = observe_original_driver_failures(monkeypatch, operation_id, session_id=harness.session_id)
        owner_trace = _observe_owner_errors(monkeypatch, operation_id)
        stopped_after_primary = _stop_after_committed_primary(
            monkeypatch, harness, operation_id, sequence=1, generation_at_least=2, count=1
        )
        actual_timing = ComposerAsyncOperationAuthority.read_continuation_timing
        observed_first = []

        def observe_first_action_fit(owner, running, *, deadline=None):
            timing = actual_timing(owner, running, deadline=deadline)
            if running.claim.operation_id != operation_id or running.claim.segment_sequence != 1 or observed_first:
                return timing
            with harness.app.state.session_engine.connect() as conn:
                selected = load_snapshot(conn, session_id=str(harness.session_id), operation_id=operation_id)
                top = decode_continuation_snapshot(selected.payload_json).frames[-1]
            if type(top) is not ComposeFrame or top.phase != "action_ready":
                return timing
            observed_first.append((timing.sequence, timing.generation, selected.checkpoint_id))
            return replace(timing, hard_remaining_seconds=0.0, cooperative_remaining_seconds=0.0, yield_pending=True)

        monkeypatch.setattr(ComposerAsyncOperationAuthority, "read_continuation_timing", observe_first_action_fit)
        terminal = await _settle_with_bounded_failure_state(
            harness,
            message_body("Explain this pipeline.", operation_id=operation_id),
            sdk,
            observed_first,
            driver_trace,
            owner_trace,
            expected_status="failed",
        )
        assert terminal.error is not None and terminal.error.error_type == "request_cancelled"
        assert observed_first and len(stopped_after_primary) == 1
        assert sdk.roles == ["primary"] and not sdk.steps
        with harness.app.state.session_engine.connect() as conn:
            (segment,) = conn.execute(select(segments).where(segments.c.operation_id == operation_id)).all()
            tags = tuple(row.tag for row in conn.execute(select(events.c.tag).where(events.c.operation_id == operation_id)))
        assert (segment.sequence, segment.state) == (1, "settled") and segment.generation >= 2
        assert "recovered" in tags and "yielded" not in tags
        primary = tuple(row for row in effects(harness.app, operation_id) if row.effect_key.startswith("provider:0:"))
        assert len(primary) == 1 and primary[0].creator_sequence == 1 and primary[0].disposition == "committed"
