"""Calibration controls for the optional original EARLY timing observer."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.continuation_retention_memo import (
    current_v4_preparation_memo,
    finish_v4_preparation_memo,
    prepare_retention_sql_callable,
    v4_preparation_memo_scope,
)
from elspeth.web.required_executor import InvocationGate, RequiredInvocationAborted, RequiredInvocationWitness
from elspeth.web.sessions.engine import create_session_engine
from tests.helpers.continuation_early_profile import PROFILE_FLAG, EarlyPreparationProfile, early_preparation_profile
from tests.helpers.continuation_policy import minimal_continuation_policy


@pytest.mark.asyncio
async def test_early_profile_absent_flag_leaves_original_clock_hooks_uninstalled(monkeypatch):
    monkeypatch.delenv(PROFILE_FLAG, raising=False)
    original_init, original_finish = ContinuationActionClock.__init__, ContinuationActionClock.finish_preparation
    engine = create_session_engine("sqlite://")
    try:
        with early_preparation_profile(monkeypatch, engine, output_key="unobserved-control") as profile:
            assert profile is None
            assert ContinuationActionClock.__init__ is original_init
            assert ContinuationActionClock.finish_preparation is original_finish
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            clock.finish_preparation()
            assert clock._seen == {"preparation"}
        assert ContinuationActionClock.__init__ is original_init
        assert ContinuationActionClock.finish_preparation is original_finish
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", (False, True), ids=("inclusive-only", "selected-spans"))
async def test_early_profile_keeps_actual_clock_policy_deadlines_stages_and_finish_error(monkeypatch, enabled):
    profile = EarlyPreparationProfile(enabled=enabled)
    policy = minimal_continuation_policy().actions.advisor
    engine = create_session_engine("sqlite://")
    original_init, original_finish = ContinuationActionClock.__init__, ContinuationActionClock.finish_preparation
    try:
        with profile.install(monkeypatch, engine):
            clock = ContinuationActionClock(policy)
            assert clock.policy is policy and clock._seen == set()
            before = (clock.started, clock.started_monotonic, clock.deadline, clock.preparation_deadline_monotonic)
            assert clock.deadline == clock.started + policy.total
            assert clock.preparation_deadline_monotonic == clock.started_monotonic + policy.preparation
            observed = profile.snapshot()
            assert clock._seen == set() and observed["clocks"][0]["finish_attempts"] == []
            assert observed["clocks"][0]["identity"] == id(clock)
            assert observed["clocks"][0]["policy_identity"] == id(policy)
            clock.finish_preparation()
            assert clock._seen == {"preparation"}
            with pytest.raises(ValueError, match="cannot renew or follow another stage"):
                clock.finish_preparation()
            assert (clock.started, clock.started_monotonic, clock.deadline, clock.preparation_deadline_monotonic) == before
            assert clock.policy is policy and clock._seen == {"preparation"}
        assert ContinuationActionClock.__init__ is original_init
        assert ContinuationActionClock.finish_preparation is original_finish
        snapshot = profile.snapshot()
        assert snapshot["mode"] == ("spans" if enabled else "inclusive_only")
        assert len(snapshot["clocks"]) == 1
        observed_clock = snapshot["clocks"][0]
        assert observed_clock["policy"] == policy.model_dump(mode="json")
        assert observed_clock["preparation_deadline_monotonic"] == before[3]
        assert observed_clock["action_deadline"] == before[2]
        attempts = observed_clock["finish_attempts"]
        assert [attempt["error"] for attempt in attempts] == [None, "ValueError"]
        assert all(attempt["same_thread"] and attempt["seen"] == ["preparation"] for attempt in attempts)
        assert all(0 <= attempt["elapsed_to_entry_ns"] <= attempt["elapsed_to_exit_ns"] for attempt in attempts)
        assert all(attempt["thread_cpu_ns"] >= 0 for attempt in attempts)
        assert not snapshot["errors"] and not snapshot["incomplete_spans"]
        assert snapshot["cpu_profile_enabled"] is False
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", (False, True), ids=("inclusive-only", "selected-spans"))
async def test_early_profile_span_primitives_preserve_original_results_and_exception_identity(monkeypatch, enabled):
    profile = EarlyPreparationProfile(enabled=enabled)
    marker = object()
    original_error = RuntimeError("private calibration exception")
    calls = []

    def successful(value):
        calls.append(value)
        return value

    async def awaited(value):
        await asyncio.sleep(0)
        calls.append(value)
        return value

    def fails():
        raise original_error

    async def fails_async():
        raise original_error

    engine = create_session_engine("sqlite://")
    try:
        with profile.install(monkeypatch, engine):
            observed = profile.spans.sync("control.sync", successful)
            observed_async = profile.spans.asynchronous("control.async", awaited)
            assert observed(marker) is marker
            assert await observed_async(marker) is marker
            with pytest.raises(RuntimeError) as synchronous:
                profile.spans.sync("control.error", fails)()
            with pytest.raises(RuntimeError) as asynchronous:
                await profile.spans.asynchronous("control.async_error", fails_async)()
            assert synchronous.value is asynchronous.value is original_error
            profile.spans.sync("control.uninvoked", successful)
        assert calls == [marker, marker]
        snapshot = profile.spans.snapshot()
        assert not snapshot["errors"] and not snapshot["incomplete_spans"]
        if enabled:
            assert [row["label"] for row in snapshot["records"]] == [
                "control.sync",
                "control.async",
                "control.error",
                "control.async_error",
            ]
            assert [row["error"] for row in snapshot["records"]] == [None, None, "RuntimeError", "RuntimeError"]
            assert all(row["elapsed_ns"] >= 0 and row["thread_cpu_ns"] >= 0 for row in snapshot["records"])
        else:
            assert observed is successful and observed_async is awaited and snapshot["records"] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("enabled", (False, True), ids=("inclusive-only", "selected-spans"))
def test_early_profile_pairs_real_sql_success_and_error_and_removes_listeners(monkeypatch, enabled):
    profile = EarlyPreparationProfile(enabled=enabled)
    engine = create_session_engine("sqlite://")
    callbacks = (
        ("before_cursor_execute", profile.spans._before_sql),
        ("after_cursor_execute", profile.spans._after_sql),
        ("handle_error", profile.spans._sql_error),
    )
    try:
        with engine.connect() as conn:
            assert conn.execute(text("select 1")).scalar_one() == 1
            with profile.install(monkeypatch, engine):
                assert all(event.contains(engine, name, callback) is enabled for name, callback in callbacks)
                assert conn.execute(text("select 2")).scalar_one() == 2
                assert conn.execute(text("select 3")).scalar_one() == 3
                with pytest.raises(DBAPIError):
                    conn.execute(text("select from"))
            assert all(not event.contains(engine, name, callback) for name, callback in callbacks)
            assert conn.execute(text("select 4")).scalar_one() == 4
        snapshot = profile.spans.snapshot()
        assert not snapshot["errors"] and not snapshot["incomplete_spans"] and snapshot["pending_sql_contexts"] == 0
        if enabled:
            rows = snapshot["records"]
            assert len(rows) == 3 and all(row["kind"] == "sql" for row in rows)
            assert [row["error"] for row in rows] == [None, None, "OperationalError"]
            assert len({row["template_sha256"] for row in rows}) == 3
            assert all("statement" not in row and "parameters" not in row for row in rows)
        else:
            assert snapshot["records"] == []
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", (False, True), ids=("inclusive-only", "selected-spans"))
async def test_early_profile_selected_wrappers_observe_only_original_active_preparation(monkeypatch, enabled):
    profile = EarlyPreparationProfile(enabled=enabled)
    marker = object()
    original_error = RuntimeError("private active-window control")
    calls = []

    def successful():
        calls.append("sync")
        return marker

    async def awaited():
        await asyncio.sleep(0)
        calls.append("async")
        return marker

    def fails():
        calls.append("error")
        raise original_error

    async def fails_async():
        calls.append("async_error")
        raise original_error

    observed = profile._sync("active.sync", successful)
    observed_async = profile._async("active.async", awaited)
    observed_error = profile._sync("active.error", fails)
    observed_async_error = profile._async("active.async_error", fails_async)
    engine = create_session_engine("sqlite://")
    try:
        with profile.install(monkeypatch, engine):
            assert observed() is marker and await observed_async() is marker
            assert profile.spans.snapshot()["records"] == []
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            before = (clock.deadline, clock.preparation_deadline_monotonic)
            assert observed() is marker and await observed_async() is marker
            with pytest.raises(RuntimeError) as synchronous:
                observed_error()
            with pytest.raises(RuntimeError) as asynchronous:
                await observed_async_error()
            assert synchronous.value is asynchronous.value is original_error
            active = profile.spans.snapshot()
            if enabled:
                assert [row["label"] for row in active["records"]] == ["active.sync", "active.async", "active.error", "active.async_error"]
                assert all(row["clock"] == id(clock) for row in active["records"])
                assert [row["error"] for row in active["records"]] == [None, None, "RuntimeError", "RuntimeError"]
            else:
                assert active["records"] == []
            assert clock._seen == set() and (clock.deadline, clock.preparation_deadline_monotonic) == before
            clock.finish_preparation()
            assert observed() is marker and await observed_async() is marker
            assert clock._seen == {"preparation"} and (clock.deadline, clock.preparation_deadline_monotonic) == before
            after = profile.spans.snapshot()
            assert after["records"] == active["records"]
            assert not after["errors"] and not after["incomplete_spans"]
        assert calls == ["sync", "async", "sync", "async", "error", "async_error", "sync", "async"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("enabled", (False, True), ids=("inclusive-only", "selected-spans"))
def test_early_profile_preserves_actual_witness_callable_gate_result_and_error(monkeypatch, enabled):
    profile = EarlyPreparationProfile(enabled=enabled)
    marker = object()
    original_error = RuntimeError("private original worker exception")
    actual_submit = ThreadPoolExecutor.submit
    submissions, calls = [], []
    main_thread = threading.get_native_id()

    def observed_submit(executor, function, /, *args, **kwargs):
        submissions.append((function, args, kwargs))
        return actual_submit(executor, function, *args, **kwargs)

    def successful():
        calls.append(("returned", threading.get_native_id()))
        return marker

    def fails():
        calls.append(("raised", threading.get_native_id()))
        raise original_error

    engine = create_session_engine("sqlite://")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(ThreadPoolExecutor, "submit", observed_submit)
            with profile.install(patch, engine), ThreadPoolExecutor(max_workers=1) as executor:
                assert executor.submit(successful).result(timeout=5) is marker  # Unowned submission stays unobserved.
                witness = RequiredInvocationWitness(1)
                original_bound = witness.invoke
                future = executor.submit(original_bound, successful)
                wait_until = time.monotonic() + 5
                while not witness.snapshot().entered and time.monotonic() < wait_until:
                    time.sleep(0.001)
                assert witness.snapshot().entered and witness.snapshot().gate is InvocationGate.WAITING
                assert not witness.snapshot().callable_started and not future.done() and len(calls) == 1
                witness.arm()
                assert future.result(timeout=5) is marker
                failed = RequiredInvocationWitness(1)
                failed_bound = failed.invoke
                failure = executor.submit(failed_bound, fails)
                failed.arm()
                with pytest.raises(RuntimeError) as raised:
                    failure.result(timeout=5)
                assert raised.value is original_error
                aborted = RequiredInvocationWitness(1)
                aborted_bound = aborted.invoke
                cancelled = executor.submit(aborted_bound, successful)
                aborted.abort()
                with pytest.raises(RequiredInvocationAborted):
                    cancelled.result(timeout=5)
        assert submissions == [
            (successful, (), {}),
            (original_bound, (successful,), {}),
            (failed_bound, (fails,), {}),
            (aborted_bound, (successful,), {}),
        ]
        assert submissions[1][0] is original_bound and submissions[1][1][0] is successful
        assert submissions[2][0] is failed_bound and submissions[2][1][0] is fails
        assert submissions[3][0] is aborted_bound and submissions[3][1][0] is successful
        assert [name for name, _thread in calls] == ["returned", "returned", "raised"]
        assert all(thread != main_thread for _name, thread in calls)
        assert witness.snapshot().callable_finished and failed.snapshot().callable_finished
        assert aborted.snapshot().valid_aborted_exit
        snapshot = profile.spans.snapshot()
        assert not snapshot["errors"] and not snapshot["incomplete_spans"] and not snapshot["unpaired_submissions"]
        if enabled:
            submitted = [row for row in snapshot["records"] if row["kind"] == "submission"]
            workers = [row for row in snapshot["records"] if row["kind"] == "worker"]
            assert len(submitted) == len(workers) == 3
            assert [row["future_identity"] for row in submitted] == [id(future), id(failure), id(cancelled)]
            assert {row["worker_id"] for row in submitted} == {row["id"] for row in workers}
            assert {row["submission"] for row in workers} == {row["id"] for row in submitted}
            assert [row["error"] for row in workers] == [None, "RuntimeError", "RequiredInvocationAborted"]
            assert all(row["queue_to_worker_ns"] >= 0 and row["thread_cpu_ns"] >= 0 for row in workers)
        else:
            assert snapshot["records"] == []
        assert ThreadPoolExecutor.submit is actual_submit
    finally:
        engine.dispose()


def test_early_profile_flush_keeps_original_exception_and_reports_output_failure(tmp_path):
    profile = EarlyPreparationProfile(enabled=True)
    original = RuntimeError("private original diagnostic exception")
    output = tmp_path / "early-profile.json"
    with pytest.raises(RuntimeError) as raised:
        try:
            with profile.spans.span("control.original_failure"):
                raise original
        finally:
            assert profile.flush(output) is None
    assert raised.value is original
    data = json.loads(output.read_text())
    assert data["observer"] == "original_early_preparation"
    assert data["records"][0]["error"] == "RuntimeError" and data["incomplete_spans"] == []
    assert "private original diagnostic exception" not in json.dumps(data)
    assert isinstance(profile.flush(output / "invalid-child.json"), OSError)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", (False, True), ids=("inclusive-only", "selected-spans"))
async def test_early_profile_tracks_original_worker_clock_through_real_sql_context_reset(monkeypatch, enabled):
    profile = EarlyPreparationProfile(enabled=enabled)
    marker = object()
    original_error = RuntimeError("private context-reset control")
    actual_submit = ThreadPoolExecutor.submit
    submissions, calls = [], []
    should_fail = False

    def observe_submission(executor, function, /, *args, **kwargs):
        submissions.append((function, args))
        return actual_submit(executor, function, *args, **kwargs)

    def original_callback():
        # The production memo adapter deliberately enters a fresh Context.
        # Worker identity must survive independently of the lost ContextVar.
        assert profile.spans._clock.get() is None
        calls.append(current_v4_preparation_memo(clock))
        if should_fail:
            raise original_error
        return marker

    observed = profile._sync("context-reset.target", original_callback)
    engine = create_session_engine("sqlite://")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(ThreadPoolExecutor, "submit", observe_submission)
            with profile.install(patch, engine), ThreadPoolExecutor(max_workers=1) as executor:
                clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
                deadlines = clock.deadline, clock.preparation_deadline_monotonic
                with v4_preparation_memo_scope(clock) as memo:
                    adapter = prepare_retention_sql_callable(observed)
                    assert executor.submit(adapter).result(timeout=5) is marker
                    assert profile.spans.snapshot()["records"] == []  # Unowned work is not a selected worker.
                    witness = RequiredInvocationWitness(1)
                    bound = witness.invoke
                    successful = executor.submit(bound, adapter)
                    witness.arm()
                    assert successful.result(timeout=5) is marker
                    should_fail = True
                    failed = RequiredInvocationWitness(1)
                    failed_bound = failed.invoke
                    failure = executor.submit(failed_bound, adapter)
                    failed.arm()
                    with pytest.raises(RuntimeError) as raised:
                        failure.result(timeout=5)
                    assert raised.value is original_error
                    active = [row for row in profile.spans.snapshot()["records"] if row["label"] == "context-reset.target"]
                    if enabled:
                        assert len(active) == 2 and [row["error"] for row in active] == [None, "RuntimeError"]
                        assert all(row["clock"] == id(clock) and row["worker"] is not None for row in active)
                    else:
                        assert active == []
                    should_fail = False
                    try:
                        clock.finish_preparation()
                    finally:
                        finish_v4_preparation_memo(clock)
                    assert not memo.active and current_v4_preparation_memo(clock) is None
                    after_cut = RequiredInvocationWitness(1)
                    after_bound = after_cut.invoke
                    following = executor.submit(after_bound, adapter)  # Reuse the exact original captured adapter.
                    after_cut.arm()
                    assert following.result(timeout=5) is marker
                    snapshot = profile.spans.snapshot()
                    assert [row for row in snapshot["records"] if row["label"] == "context-reset.target"] == active
                    assert not snapshot["errors"] and not snapshot["incomplete_spans"] and not snapshot["unpaired_submissions"]
                    assert (clock.deadline, clock.preparation_deadline_monotonic) == deadlines and clock._seen == {"preparation"}
        assert calls == [memo, memo, memo, None]
        assert submissions[0] == (adapter, ()) and submissions[0][0] is adapter
        for submitted, original_bound in zip(submissions[1:], (bound, failed_bound, after_bound), strict=True):
            assert submitted[0] is original_bound and submitted[1][0] is adapter
        assert witness.snapshot().callable_finished and failed.snapshot().callable_finished and after_cut.snapshot().callable_finished
        assert ThreadPoolExecutor.submit is actual_submit
    finally:
        engine.dispose()
