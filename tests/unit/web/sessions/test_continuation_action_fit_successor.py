"""A later Compose action-fit yield creates a cold successor without replay."""

from __future__ import annotations

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
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
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


async def _settle_with_bounded_failure_state(harness, body, sdk, timing, driver_trace, owner_trace, proof_profile=None):
    try:
        terminal = await submit_and_settle(harness, body, rounds=200)
    except AssertionError as original:
        if str(original) == "Continuation request did not settle":
            original.add_note(
                f"bounded owner state: {_failure_state(harness, body['operation_id'], sdk, timing, driver_trace, owner_trace, proof_profile)!r}"
            )
        raise
    if terminal.status != "completed":
        pytest.fail(
            f"continuation terminal failed: {(terminal.status, terminal.error.failure_code if terminal.error else None)!r}; "
            f"{_failure_state(harness, body['operation_id'], sdk, timing, driver_trace, owner_trace, proof_profile)!r}",
            pytrace=False,
        )
    return terminal


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
