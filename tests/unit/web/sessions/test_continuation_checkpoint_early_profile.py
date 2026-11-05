"""Calibration controls for the optional original EARLY timing observer."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_clock as clock_module
from elspeth.web.composer import continuation_current_evidence as evidence_bridge
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.continuation_retention_memo import (
    CanonicalTextVerificationKey,
    borrow_v4_preparation_memo_scope,
    current_v4_preparation_memo,
    finish_v4_preparation_memo,
    prepare_retention_sql_callable,
    v4_preparation_memo_scope,
)
from elspeth.web.required_executor import InvocationGate, RequiredInvocationAborted, RequiredInvocationWitness
from elspeth.web.sessions.engine import create_session_engine
from tests.helpers.continuation_early_profile import (
    PROFILE_FLAG,
    SEMANTIC_PROFILE_DIRECTORY,
    SEMANTIC_PROFILE_FLAG,
    EarlyPreparationProfile,
    SemanticPreparationProfile,
    _semantic_preparation_window,
    early_preparation_profile,
    semantic_preparation_profile,
)
from tests.helpers.continuation_policy import minimal_continuation_policy


@pytest.mark.asyncio
async def test_semantic_profile_absent_flag_keeps_original_clock_and_aliases(monkeypatch):
    from elspeth.web.composer import continuation_semantic_advisor_phase as phase

    monkeypatch.delenv(SEMANTIC_PROFILE_FLAG, raising=False)
    original_clock = ContinuationActionClock.__init__
    original_request = phase.prepare_semantic_advisor_request
    engine = create_session_engine("sqlite://")
    try:
        with semantic_preparation_profile(monkeypatch, engine, output_key="disabled-semantic-control") as profile:
            assert profile is None
            assert ContinuationActionClock.__init__ is original_clock
            assert phase.prepare_semantic_advisor_request is original_request
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            clock.finish_preparation()
            assert clock._seen == {"preparation"}
        assert ContinuationActionClock.__init__ is original_clock
        assert phase.prepare_semantic_advisor_request is original_request
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("0", "2"))
async def test_semantic_profile_context_keeps_original_finish_error_when_output_is_unavailable(monkeypatch, mode):
    monkeypatch.setenv(SEMANTIC_PROFILE_FLAG, mode)
    monkeypatch.setenv(SEMANTIC_PROFILE_DIRECTORY, ".claude/lanes/semantic-profile-unwritten-control")
    snapshots, originals = [], []

    def flush(profile, path):
        assert path.name == "same-original-failure.json"
        snapshots.append(profile.snapshot())
        return OSError("controlled diagnostic output refusal")

    monkeypatch.setattr(SemanticPreparationProfile, "flush", flush)
    engine = create_session_engine("sqlite://")
    try:
        with (
            pytest.raises(ValueError) as raised,
            semantic_preparation_profile(monkeypatch, engine, output_key="same-original-failure") as profile,
        ):
            assert type(profile) is SemanticPreparationProfile and not profile.cpu_profile and profile.spans.text_memo is None
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            profile.select_clock(clock)
            deadlines = clock.deadline, clock.preparation_deadline_monotonic
            clock.finish_preparation()
            try:
                clock.finish_preparation()
            except ValueError as original:
                originals.append(original)
                raise
        assert raised.value is originals[0] and clock._seen == {"preparation"}
        assert (clock.deadline, clock.preparation_deadline_monotonic) == deadlines
        assert len(snapshots) == 1
        assert snapshots[0]["case_outcome"] == {"status": "raised", "error": "ValueError"}
        assert snapshots[0]["mode"] == ("inclusive_only" if mode == "0" else "spans_only")
        assert not snapshots[0]["preparation"]["valid"]  # A second finish cannot renew the one measured window.
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", (False, True), ids=("inclusive-only", "selected-spans"))
async def test_semantic_profile_observes_original_clock_cpu_async_aliases_and_only_selected_window(monkeypatch, enabled):
    from elspeth.web.composer import continuation_semantic_advisor as semantic
    from elspeth.web.composer import continuation_semantic_advisor_phase as phase
    from elspeth.web.sessions import continuation_semantic_advisor as sql_semantic

    profile = SemanticPreparationProfile(enabled=enabled)
    marker = object()
    original_error = RuntimeError("private semantic async control")

    def source():
        return marker

    async def asynchronous(*, fails=False):
        await asyncio.sleep(0)
        if fails:
            raise original_error
        return marker

    observed = profile._sync("semantic.control.source", source)
    awaited = profile._async("semantic.control.async", asynchronous)
    originals = (phase.prepare_semantic_advisor_request, sql_semantic.read_runtime_source_on_connection, semantic.run_semantic_advisor)
    engine = create_session_engine("sqlite://")
    try:
        with profile.install(monkeypatch, engine):
            current = (
                phase.prepare_semantic_advisor_request,
                sql_semantic.read_runtime_source_on_connection,
                semantic.run_semantic_advisor,
            )
            for actual, original in zip(current, originals, strict=True):
                if enabled:
                    assert actual.__wrapped__ is original
                else:
                    assert actual is original
            assert observed() is marker and await awaited() is marker
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            assert observed() is marker  # Constructed but not explicitly selected.
            assert profile.spans.records == []
            profile.select_clock(clock)
            deadlines = clock.deadline, clock.preparation_deadline_monotonic
            assert observed() is marker and await awaited() is marker
            with pytest.raises(RuntimeError) as raised:
                await awaited(fails=True)
            assert raised.value is original_error
            active = profile.snapshot()
            assert [row["label"] for row in active["records"]] == (
                ["semantic.control.source", "semantic.control.async", "semantic.control.async"] if enabled else []
            )
            assert not active["preparation"]["valid"]  # No actual finish yet.
            clock.finish_preparation()
            assert observed() is marker and await awaited() is marker
            other = ContinuationActionClock(clock.policy)
            assert observed() is marker and await awaited() is marker
            other.finish_preparation()
            assert profile.spans.records == active["records"]
            assert clock._seen == {"preparation"} and (clock.deadline, clock.preparation_deadline_monotonic) == deadlines
        snapshot = profile.snapshot()
        assert snapshot["preparation"]["valid"] and snapshot["preparation"]["clock"] == id(clock)
        assert snapshot["observer"] == "original_initial_semantic_preparation"
        assert snapshot["cpu_profile_enabled"] is False and snapshot["cpu_profiles"] == [] and snapshot["text_memo"] is None
        assert snapshot["mode"] == ("spans_only" if enabled else "inclusive_only")
        original_clock = next(row for row in snapshot["clocks"] if row["identity"] == id(clock))
        finish = original_clock["finish_attempts"][0]
        assert original_clock["start_ns"] <= original_clock["cpu_samples_start"]["wall_before_ns"]
        assert original_clock["cpu_samples_start"]["wall_after_ns"] <= finish["entry_ns"]
        assert finish["entry_ns"] <= finish["cpu_samples_entry"]["wall_before_ns"] <= finish["cpu_samples_entry"]["wall_after_ns"]
        assert finish["entry_process_cpu_ns"] >= 0 and finish["entry_thread_cpu_ns"] >= 0
        assert finish["same_thread"] and finish["error"] is None and finish["seen"] == ["preparation"]
        assert (
            phase.prepare_semantic_advisor_request,
            sql_semantic.read_runtime_source_on_connection,
            semantic.run_semantic_advisor,
        ) == originals
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_semantic_profile_pairs_real_context_reset_workers_sql_and_rejects_window_corruption(monkeypatch):
    profile = SemanticPreparationProfile(enabled=True)
    marker = object()
    original_error = RuntimeError("private semantic worker control")
    actual_submit = ThreadPoolExecutor.submit
    submitted, calls = [], []
    should_fail = False

    def submit(executor, function, /, *args, **kwargs):
        submitted.append((function, args))
        return actual_submit(executor, function, *args, **kwargs)

    engine = create_session_engine("sqlite://")

    def body():
        assert profile.spans._clock.get() is None
        calls.append(current_v4_preparation_memo(clock))
        with engine.connect() as conn:
            assert conn.execute(text("select :value"), {"value": "PRIVATE_SEMANTIC_PARAMETER"}).scalar_one() == "PRIVATE_SEMANTIC_PARAMETER"
        if should_fail:
            raise original_error
        return marker

    observed = profile._sync("semantic.control.worker_body", body)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(ThreadPoolExecutor, "submit", submit)
            with profile.install(patch, engine), ThreadPoolExecutor(max_workers=1) as executor:
                clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
                profile.select_clock(clock)
                with v4_preparation_memo_scope(clock) as memo:
                    adapter = prepare_retention_sql_callable(observed)
                    first = RequiredInvocationWitness(1)
                    first_bound = first.invoke
                    returned = executor.submit(first_bound, adapter)
                    first.arm()
                    assert returned.result(timeout=5) is marker
                    should_fail = True
                    second = RequiredInvocationWitness(1)
                    second_bound = second.invoke
                    failed = executor.submit(second_bound, adapter)
                    second.arm()
                    with pytest.raises(RuntimeError) as raised:
                        failed.result(timeout=5)
                    assert raised.value is original_error
                    should_fail = False
                    clock.finish_preparation()
                    after = RequiredInvocationWitness(1)
                    after_bound = after.invoke
                    postfinish = executor.submit(after_bound, adapter)
                    after.arm()
                    assert postfinish.result(timeout=5) is marker
        snapshot = profile.snapshot()
        assert snapshot["preparation"]["valid"]
        assert calls == [memo, memo, memo]
        assert submitted == [(first_bound, (adapter,)), (second_bound, (adapter,)), (after_bound, (adapter,))]
        assert first.snapshot().callable_finished and second.snapshot().callable_finished and after.snapshot().callable_finished
        target = [row for row in snapshot["records"] if row["label"] == "semantic.control.worker_body"]
        assert len(target) == 2 and [row["error"] for row in target] == [None, "RuntimeError"]
        assert all(row["clock"] == id(clock) and row["worker"] is not None and row["thread_cpu_ns"] >= 0 for row in target)
        assert len(snapshot["preparation"]["preparation_submission_ids"]) == 2
        assert snapshot["preparation"]["postfinish_record_ids"]
        assert snapshot["pending_sql_contexts"] == 0 and snapshot["incomplete_spans"] == snapshot["unpaired_submissions"] == []
        assert "PRIVATE_SEMANTIC_PARAMETER" not in json.dumps(snapshot)
        assert ThreadPoolExecutor.submit is actual_submit
        for fault in ("missing_worker", "wrong_clock", "outside_window", "unfinished"):
            mutant = deepcopy(snapshot)
            submission_id = mutant["preparation"]["preparation_submission_ids"][0]
            submission = next(row for row in mutant["records"] if row["id"] == submission_id)
            worker = next(row for row in mutant["records"] if row["id"] == submission["worker_id"])
            if fault == "missing_worker":
                mutant["records"].remove(worker)
            elif fault == "wrong_clock":
                worker["clock"] = -1
            elif fault == "outside_window":
                worker["end_ns"] = mutant["preparation"]["finish_entry_ns"] + 1
            else:
                mutant["incomplete_spans"].append(worker["id"])
            assert not _semantic_preparation_window(mutant, id(clock))["valid"], fault
        assert _semantic_preparation_window(snapshot, id(clock))["valid"]
    finally:
        engine.dispose()


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


def test_early_cpu_profile_is_disabled_by_default_and_does_not_replace_evidence_callable(monkeypatch):
    profile = EarlyPreparationProfile(enabled=True)
    original_scope = evidence_bridge.borrow_v4_preparation_memo_scope
    original_callable = evidence_bridge._restore_current_message_evidence
    engine = create_session_engine("sqlite://")
    try:
        with profile.install(monkeypatch, engine):
            assert evidence_bridge.borrow_v4_preparation_memo_scope is original_scope
            assert evidence_bridge._restore_current_message_evidence is original_callable
        snapshot = profile.snapshot()
        assert snapshot["cpu_profile_enabled"] is False and snapshot["cpu_profiles"] == []
        assert snapshot["text_memo"] is not None
        assert set(snapshot["text_memo"]["families"]) == {"full", "canonical", "block", "request", "credential", "evidence"}
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cpu_selection", ("current_evidence", "all_workers"))
async def test_early_cpu_profile_selects_original_work_across_real_context_reset_workers(monkeypatch, cpu_selection):
    profile = EarlyPreparationProfile(
        enabled=True, cpu_profile=True, cpu_selection=cpu_selection, observe_memo=cpu_selection == "current_evidence"
    )
    original_callable = evidence_bridge._assemble_runtime_message_evidence
    original_scope = evidence_bridge.borrow_v4_preparation_memo_scope
    actual_submit = ThreadPoolExecutor.submit
    marker = object()
    submissions, calls, verified_text = [], [], []
    previous_hook = sys.getprofile()
    tool = sys.monitoring.PROFILER_ID
    assert sys.monitoring.get_tool(tool) is None

    def observed_submit(executor, function, /, *args, **kwargs):
        submissions.append((function, args))
        return actual_submit(executor, function, *args, **kwargs)

    def controlled_body():
        calls.append(threading.get_native_id())
        assert sum(range(100)) == 4950
        return marker

    def verify_text(value):
        assert value == "{}"
        verified_text.append(value)

    def original_callback():
        assert profile.spans._clock.get() is None
        for _ in range(2):
            with evidence_bridge.borrow_v4_preparation_memo_scope() as child:
                assert child._text_parent is parent
                child.canonical_text_verification(CanonicalTextVerificationKey("{}", 100), verify_text)
                assert controlled_body() is marker
        return marker

    engine = create_session_engine("sqlite://")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(ThreadPoolExecutor, "submit", observed_submit)
            with profile.install(patch, engine), ThreadPoolExecutor(max_workers=1) as executor:
                assert evidence_bridge._assemble_runtime_message_evidence is original_callable
                if cpu_selection == "all_workers":
                    assert evidence_bridge.borrow_v4_preparation_memo_scope is original_scope
                clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
                deadlines = clock.deadline, clock.preparation_deadline_monotonic
                with v4_preparation_memo_scope(clock) as parent:
                    adapter = prepare_retention_sql_callable(original_callback)
                    first, second = RequiredInvocationWitness(1), RequiredInvocationWitness(1)
                    first_bound, second_bound = first.invoke, second.invoke
                    one = executor.submit(first_bound, adapter)
                    first.arm()
                    assert one.result(timeout=5) is marker
                    two = executor.submit(second_bound, adapter)
                    second.arm()
                    assert two.result(timeout=5) is marker
                    pending = profile.snapshot()
                    assert pending["cpu_capture_valid"] is False and pending["cpu_capture_status"] == "partial_invalid"
                    assert pending["clocks"][0]["finish_attempts"] == [] and clock._seen == set()
                    clock.finish_preparation()
                    finish_v4_preparation_memo(clock)
                assert executor.submit(sys.getprofile).result(timeout=5) is None
                assert (clock.deadline, clock.preparation_deadline_monotonic) == deadlines and clock._seen == {"preparation"}
        assert evidence_bridge._assemble_runtime_message_evidence is original_callable
        assert submissions[0][0] is first_bound and submissions[0][1][0] is adapter
        assert submissions[1][0] is second_bound and submissions[1][1][0] is adapter
        assert first.snapshot().callable_finished and second.snapshot().callable_finished and len(calls) == 4
        snapshot = profile.snapshot()
        assert snapshot["cpu_capture_valid"] is True and snapshot["cpu_capture_status"] == "captured_preparation"
        assert snapshot["cpu_target_clocks"] == [id(clock)]
        assert len(snapshot["cpu_profiles"]) == (1 if cpu_selection == "current_evidence" else 2)
        captured = snapshot["cpu_profiles"][0]
        assert captured["clock"] == id(clock) and captured["witness"] == first.identity
        assert captured["within_preparation_window"] is True
        assert captured["timer"] == "time.thread_time_ns" and captured["timeunit"] == 1e-9
        if cpu_selection == "current_evidence":
            assert "original current-evidence memo scope" in captured["scope"]
            assert captured["selected_method"] == {
                "module": original_callable.__module__,
                "qualname": original_callable.__qualname__,
                "file": original_callable.__code__.co_filename,
                "line": original_callable.__code__.co_firstlineno,
            }
            assert snapshot["text_memo"]["families"]["canonical"]["requests"] == 4
            assert snapshot["text_memo"]["families"]["canonical"]["hits"] == 3
            assert snapshot["text_memo"]["observer_errors"] == 0
        else:
            assert snapshot["mode"] == "spans_only" and snapshot["text_memo"] is None
            assert snapshot["cpu_selection"] == "all_workers"
            assert {item["witness"] for item in snapshot["cpu_profiles"]} == {first.identity, second.identity}
            assert snapshot["cpu_expected_workers"] == [item["worker_id"] for item in snapshot["cpu_profiles"]]
            for item in snapshot["cpu_profiles"]:
                assert item["selected_method"] is None and "original worker including gate" in item["scope"]
                assert item["within_preparation_window"]
                assert "controlled_body" in {entry["function"] for entry in item["entries"]}
            missing_worker = profile.spans._cpu_profiles.pop()[0]["id"]
            incomplete = profile.snapshot()
            assert incomplete["cpu_capture_valid"] is False
            assert incomplete["cpu_missing_workers"] == [missing_worker]
        assert "controlled_body" in {entry["function"] for entry in captured["entries"]}
        assert verified_text == ["{}"]
        assert not snapshot["errors"] and not snapshot["incomplete_spans"] and not snapshot["unpaired_submissions"]
        assert sys.getprofile() is previous_hook and sys.monitoring.get_tool(tool) is None
        assert sys.monitoring.get_events(tool) == 0
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("unowned", "foreign_clock", "after_finish", "aborted_gate"))
async def test_early_all_worker_cpu_preserves_selection_and_aborted_gate(monkeypatch, kind):
    profile = EarlyPreparationProfile(enabled=True, cpu_profile=True, cpu_selection="all_workers", observe_memo=False)
    marker = object()
    calls = []
    previous_hook = sys.getprofile()
    tool = sys.monitoring.PROFILER_ID
    assert sys.monitoring.get_tool(tool) is None

    def target_body():
        calls.append("target")
        return marker

    def other_body():
        calls.append("other")
        return marker

    engine = create_session_engine("sqlite://")
    try:
        with profile.install(monkeypatch, engine), ThreadPoolExecutor(max_workers=1) as executor:
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            deadlines = clock.deadline, clock.preparation_deadline_monotonic
            with profile.spans.span("calibration.all_workers"):
                first = RequiredInvocationWitness(1)
                future = executor.submit(first.invoke, target_body)
                first.arm()
                assert future.result(timeout=5) is marker
            if kind == "after_finish":
                clock.finish_preparation()
            elif kind == "foreign_clock":
                foreign = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            if kind == "unowned":
                assert executor.submit(other_body).result(timeout=5) is marker
            else:
                second = RequiredInvocationWitness(1)
                other = executor.submit(second.invoke, other_body)
                if kind == "aborted_gate":
                    second.abort()
                    with pytest.raises(RequiredInvocationAborted):
                        other.result(timeout=5)
                    assert second.snapshot().valid_aborted_exit
                else:
                    second.arm()
                    assert other.result(timeout=5) is marker
            if kind == "foreign_clock":
                foreign.finish_preparation()
            if kind != "after_finish":
                clock.finish_preparation()
            assert (clock.deadline, clock.preparation_deadline_monotonic) == deadlines
            assert clock._seen == {"preparation"}
        snapshot = profile.snapshot()
        assert snapshot["cpu_capture_valid"] and snapshot["cpu_missing_workers"] == []
        assert snapshot["cpu_target_clocks"] == [id(clock)]
        assert snapshot["mode"] == "spans_only" and snapshot["text_memo"] is None
        assert len(snapshot["cpu_profiles"]) == (2 if kind == "aborted_gate" else 1)
        assert snapshot["cpu_profiles"][0]["parent_context"] == ["calibration.all_workers"]
        assert calls == (["target"] if kind == "aborted_gate" else ["target", "other"])
        assert "target_body" in {entry["function"] for entry in snapshot["cpu_profiles"][0]["entries"]}
        assert all("other_body" not in {entry["function"] for entry in item["entries"]} for item in snapshot["cpu_profiles"])
        assert not snapshot["errors"] and not snapshot["incomplete_spans"] and not snapshot["unpaired_submissions"]
        assert sys.getprofile() is previous_hook and sys.monitoring.get_tool(tool) is None
        assert sys.monitoring.get_events(tool) == 0
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("unowned", "other_module", "after_finish", "aborted_gate", "never_entered", "foreign_clock"))
async def test_early_cpu_profile_refuses_unselected_or_closed_original_work(monkeypatch, kind):
    profile = EarlyPreparationProfile(enabled=True, cpu_profile=True)
    marker = object()
    calls = []

    def selected_callback():
        assert profile.spans._clock.get() is None
        with evidence_bridge.borrow_v4_preparation_memo_scope():
            calls.append("selected")
        return marker

    def other_scope_callback():
        with borrow_v4_preparation_memo_scope():
            calls.append("other_module")
        return marker

    def no_scope_callback():
        calls.append("no_scope")
        return marker

    engine = create_session_engine("sqlite://")
    try:
        with profile.install(monkeypatch, engine), ThreadPoolExecutor(max_workers=1) as executor:
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            with v4_preparation_memo_scope(clock):
                callback = (
                    other_scope_callback if kind == "other_module" else no_scope_callback if kind == "never_entered" else selected_callback
                )
                adapter = prepare_retention_sql_callable(callback)
                if kind == "after_finish":
                    clock.finish_preparation()
                    finish_v4_preparation_memo(clock)
                if kind == "unowned":
                    assert executor.submit(adapter).result(timeout=5) is marker
                elif kind != "foreign_clock":
                    witness = RequiredInvocationWitness(1)
                    submitted = executor.submit(witness.invoke, adapter)
                    if kind == "aborted_gate":
                        witness.abort()
                        with pytest.raises(RequiredInvocationAborted):
                            submitted.result(timeout=5)
                        assert witness.snapshot().valid_aborted_exit and calls == []
                    else:
                        witness.arm()
                        assert submitted.result(timeout=5) is marker and witness.snapshot().callable_finished
            if kind == "foreign_clock":
                foreign = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
                with v4_preparation_memo_scope(foreign):
                    foreign_adapter = prepare_retention_sql_callable(selected_callback)
                    witness = RequiredInvocationWitness(1)
                    submitted = executor.submit(witness.invoke, foreign_adapter)
                    witness.arm()
                    assert submitted.result(timeout=5) is marker
                    foreign.finish_preparation()
                    finish_v4_preparation_memo(foreign)
            if kind != "after_finish":
                clock.finish_preparation()
            assert clock._seen == {"preparation"}
        snapshot = profile.snapshot()
        assert snapshot["cpu_capture_valid"] is False and snapshot["cpu_capture_status"] == "invalid"
        assert snapshot["cpu_profiles"] == [] and snapshot["cpu_target_clocks"] == [id(clock)]
        assert "CPU capture did not reach the original current-evidence memo scope" in snapshot["cpu_capture_issues"]
        assert not snapshot["incomplete_spans"] and not snapshot["unpaired_submissions"]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_early_cpu_profile_refuses_existing_worker_hook_without_replacing_it(monkeypatch):
    profile = EarlyPreparationProfile(enabled=True, cpu_profile=True)
    marker = object()
    before = sys.getprofile()

    def existing_hook(_frame, _event, _argument):
        return None

    def initialize():
        sys.setprofile(existing_hook)

    def original_callback():
        with evidence_bridge.borrow_v4_preparation_memo_scope():
            assert sys.getprofile() is existing_hook
        return marker

    engine = create_session_engine("sqlite://")
    try:
        with profile.install(monkeypatch, engine), ThreadPoolExecutor(max_workers=1, initializer=initialize) as executor:
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            with v4_preparation_memo_scope(clock):
                adapter = prepare_retention_sql_callable(original_callback)
                witness = RequiredInvocationWitness(1)
                future = executor.submit(witness.invoke, adapter)
                witness.arm()
                assert future.result(timeout=5) is marker
                assert executor.submit(sys.getprofile).result(timeout=5) is existing_hook
                executor.submit(sys.setprofile, None).result(timeout=5)
                clock.finish_preparation()
                finish_v4_preparation_memo(clock)
        snapshot = profile.snapshot()
        assert snapshot["cpu_capture_valid"] is False and snapshot["cpu_capture_status"] == "invalid"
        assert snapshot["cpu_profiles"] == []
        assert "worker CPU profile refused an existing thread hook" in snapshot["errors"]
        assert witness.snapshot().callable_finished and sys.getprofile() is before
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_early_cpu_profile_keeps_original_scope_exception_partial_until_original_finish(monkeypatch):
    profile = EarlyPreparationProfile(enabled=True, cpu_profile=True)
    original = RuntimeError("private selected original scope error")

    def original_callback():
        with evidence_bridge.borrow_v4_preparation_memo_scope():
            raise original

    engine = create_session_engine("sqlite://")
    try:
        with profile.install(monkeypatch, engine), ThreadPoolExecutor(max_workers=1) as executor:
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            deadlines = clock.deadline, clock.preparation_deadline_monotonic
            with v4_preparation_memo_scope(clock):
                adapter = prepare_retention_sql_callable(original_callback)
                witness = RequiredInvocationWitness(1)
                future = executor.submit(witness.invoke, adapter)
                witness.arm()
                with pytest.raises(RuntimeError) as raised:
                    future.result(timeout=5)
                assert raised.value is original and witness.snapshot().callable_finished
                pending = profile.snapshot()
                assert pending["cpu_capture_valid"] is False and pending["cpu_capture_status"] == "partial_invalid"
                assert len(pending["cpu_profiles"]) == 1 and pending["clocks"][0]["finish_attempts"] == []
                assert clock._seen == set() and (clock.deadline, clock.preparation_deadline_monotonic) == deadlines
                clock.finish_preparation()
                finish_v4_preparation_memo(clock)
        snapshot = profile.snapshot()
        assert snapshot["cpu_capture_valid"] is False and snapshot["cpu_capture_status"] == "partial_invalid"
        assert len(snapshot["clocks"][0]["finish_attempts"]) == 1
        assert snapshot["clocks"][0]["finish_attempts"][0]["error"] is None
        assert "private selected original scope error" not in json.dumps(snapshot)
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_raises", (False, True), ids=("valid-third-attempt", "original-third-error"))
async def test_early_cpu_profile_preserves_rejected_attempts_without_ending_original_preparation(monkeypatch, target_raises):
    profile = EarlyPreparationProfile(enabled=True, cpu_profile=True)
    refused = (AuditIntegrityError("first original refusal"), AuditIntegrityError("second original refusal"))
    target_error = RuntimeError("original selected third attempt error")
    marker = object()
    attempts, submitted, adapters, witnesses = [], [], [], []
    actual_submit = ThreadPoolExecutor.submit

    def observe_submission(executor, function, /, *args, **kwargs):
        submitted.append((function, args))
        return actual_submit(executor, function, *args, **kwargs)

    def selected_body():
        assert profile.spans._clock.get() is None
        with evidence_bridge.borrow_v4_preparation_memo_scope() as child:
            assert child._text_parent is parent
            if target_raises:
                raise target_error
            return marker

    async def original_binder_attempt():
        ordinal = len(attempts)
        attempts.append(ordinal)
        if ordinal < 2:
            raise refused[ordinal]
        adapter = prepare_retention_sql_callable(selected_body)
        adapters.append(adapter)
        witness = RequiredInvocationWitness(1)
        witnesses.append(witness)
        bound = witness.invoke
        future = executor.submit(bound, adapter)
        assert submitted[-1][0] is bound and submitted[-1][1][0] is adapter
        witness.arm()
        return future.result(timeout=5)

    observed_attempt = profile._async("calibration.binder", original_binder_attempt, preparation_entry=True)
    engine = create_session_engine("sqlite://")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(ThreadPoolExecutor, "submit", observe_submission)
            with profile.install(patch, engine), ThreadPoolExecutor(max_workers=1) as executor:
                clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
                deadlines = clock.deadline, clock.preparation_deadline_monotonic
                with v4_preparation_memo_scope(clock) as parent:
                    for refusal in refused:
                        with pytest.raises(AuditIntegrityError) as raised:
                            await observed_attempt()
                        assert raised.value is refusal
                    rejected = profile.snapshot()
                    assert rejected["cpu_profiles"] == [] and rejected["clocks"][0]["finish_attempts"] == []
                    assert rejected["cpu_target_boundaries"][0]["boundary"] is None
                    assert [attempt["kind"] for attempt in rejected["binder_attempts"]] == ["binder_attempt_raised"] * 2
                    if target_raises:
                        with pytest.raises(RuntimeError) as third:
                            await observed_attempt()
                        assert third.value is target_error
                    else:
                        assert await observed_attempt() is marker
                    assert len(adapters) == len(witnesses) == len(submitted) == 1
                    assert witnesses[0].snapshot().callable_finished and attempts == [0, 1, 2]
                    assert clock._seen == set() and (clock.deadline, clock.preparation_deadline_monotonic) == deadlines
                    clock.finish_preparation()
                    finish_v4_preparation_memo(clock)
        snapshot = profile.snapshot()
        assert len(snapshot["cpu_profiles"]) == 1
        assert snapshot["cpu_profiles"][0]["clock"] == id(clock)
        assert snapshot["cpu_profiles"][0]["witness"] == witnesses[0].identity
        assert snapshot["cpu_capture_valid"] is (not target_raises)
        assert snapshot["cpu_capture_status"] == ("partial_invalid" if target_raises else "captured_preparation")
        assert [attempt["kind"] for attempt in snapshot["binder_attempts"]] == [
            "binder_attempt_raised",
            "binder_attempt_raised",
            "binder_attempt_raised" if target_raises else "binder_attempt_returned",
        ]
        assert [attempt["error"] for attempt in snapshot["binder_attempts"]] == [
            "AuditIntegrityError",
            "AuditIntegrityError",
            "RuntimeError" if target_raises else None,
        ]
        assert all(attempt["clock"] == id(clock) and attempt["label"] == "calibration.binder" for attempt in snapshot["binder_attempts"])
        assert len(snapshot["clocks"][0]["finish_attempts"]) == 1
        assert not snapshot["errors"] and not snapshot["incomplete_spans"] and not snapshot["unpaired_submissions"]
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ("finished_then_refusal", "timed_out_finish", "closed_parent"))
async def test_early_cpu_profile_actual_finish_or_closed_creator_blocks_later_scope(monkeypatch, boundary):
    profile = EarlyPreparationProfile(enabled=True, cpu_profile=True)
    marker = object()
    refusal = AuditIntegrityError("original refusal after actual finish")
    calls = []

    def selected_body():
        assert profile.spans._clock.get() is None
        with evidence_bridge.borrow_v4_preparation_memo_scope():
            calls.append(marker)
        return marker

    async def refused_attempt():
        raise refusal

    engine = create_session_engine("sqlite://")
    try:
        with profile.install(monkeypatch, engine), ThreadPoolExecutor(max_workers=1) as executor:
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            deadlines = clock.deadline, clock.preparation_deadline_monotonic
            with v4_preparation_memo_scope(clock) as parent:
                adapter = prepare_retention_sql_callable(selected_body)
                if boundary == "finished_then_refusal":
                    clock.finish_preparation()
                    observed = profile._async("calibration.refused-after-finish", refused_attempt, preparation_entry=True)
                    with pytest.raises(AuditIntegrityError) as raised:
                        await observed()
                    assert raised.value is refusal and parent.active
                elif boundary == "timed_out_finish":
                    # Change only the original monotonic observation at the
                    # actual finish call; the clock and its bounds stay intact.
                    with monkeypatch.context() as observation:
                        observation.setattr(clock_module.time, "monotonic", lambda: deadlines[1] + 0.001)
                        with pytest.raises(TimeoutError, match="continuation preparation allowance expired"):
                            clock.finish_preparation()
                    assert clock._seen == set() and parent.active
                else:
                    parent.close()
                    assert not parent.active and clock._seen == set()
                witness = RequiredInvocationWitness(1)
                future = executor.submit(witness.invoke, adapter)
                witness.arm()
                assert future.result(timeout=5) is marker and witness.snapshot().callable_finished
                assert calls == [marker] and (clock.deadline, clock.preparation_deadline_monotonic) == deadlines
                if boundary == "closed_parent":
                    assert profile.snapshot()["clocks"][0]["finish_attempts"] == []
                    clock.finish_preparation()
                snapshot = profile.snapshot()
                assert snapshot["cpu_capture_valid"] is False and snapshot["cpu_capture_status"] == "invalid"
                assert snapshot["cpu_profiles"] == []
                finish_attempts = snapshot["clocks"][0]["finish_attempts"]
                assert len(finish_attempts) == 1
                assert finish_attempts[0]["error"] == ("TimeoutError" if boundary == "timed_out_finish" else None)
                assert finish_attempts[0]["seen"] == ([] if boundary == "timed_out_finish" else ["preparation"])
                assert not snapshot["errors"] and not snapshot["incomplete_spans"] and not snapshot["unpaired_submissions"]
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("first_only", "exception", "foreign_clock", "unowned", "no_parent", "after_finish", "existing_hook"))
async def test_message_inputs_cpu_selects_one_actual_worker_call_and_restores_hooks(monkeypatch, case):
    from contextlib import nullcontext

    from elspeth.web.composer import continuation_prompt_producers as producers

    profile = EarlyPreparationProfile(enabled=True, cpu_profile=True, cpu_selection="message_inputs", observe_memo=False)
    marker = object()
    failure = RuntimeError("controlled message inputs failure")
    calls = []
    before = sys.getprofile()
    tool = sys.monitoring.PROFILER_ID
    assert sys.monitoring.get_tool(tool) is None

    def existing_hook(_frame, _event, _argument):
        return None

    def target():
        calls.append(threading.get_native_id())
        assert sum(range(100)) == 4950
        if case == "exception":
            raise failure
        return marker

    def earlier_work():
        return sum(range(10))

    def later_work():
        return sum(range(20))

    def callback():
        assert profile.spans._clock.get() == (id(clock) if case == "no_parent" else None)
        earlier_work()
        assert producers._inputs_from_current_evidence_on_connection() is marker
        later_work()
        assert producers._inputs_from_current_evidence_on_connection() is marker
        return marker

    engine = create_session_engine("sqlite://")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(producers, "_inputs_from_current_evidence_on_connection", target)
            with profile.install(patch, engine), ThreadPoolExecutor(max_workers=1) as executor:
                clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
                selected_clock = (
                    ContinuationActionClock(minimal_continuation_policy().actions.advisor) if case == "foreign_clock" else clock
                )
                with nullcontext() if case == "no_parent" else v4_preparation_memo_scope(selected_clock):
                    adapter = prepare_retention_sql_callable(callback)
                    if case == "after_finish":
                        clock.finish_preparation()
                        finish_v4_preparation_memo(clock)
                    if case == "existing_hook":
                        executor.submit(sys.setprofile, existing_hook).result(timeout=5)
                    witness = RequiredInvocationWitness(1)
                    if case == "unowned":
                        future = executor.submit(adapter)
                    else:
                        future = executor.submit(witness.invoke, adapter)
                        witness.arm()
                    if case == "exception":
                        with pytest.raises(RuntimeError) as raised:
                            future.result(timeout=5)
                        assert raised.value is failure
                    else:
                        assert future.result(timeout=5) is marker
                    assert executor.submit(sys.getprofile).result(timeout=5) is (existing_hook if case == "existing_hook" else None)
                    if case == "existing_hook":
                        executor.submit(sys.setprofile, None).result(timeout=5)
                    if case != "after_finish":
                        clock.finish_preparation()
                    if selected_clock is not clock:
                        selected_clock.finish_preparation()
                    finish_v4_preparation_memo(selected_clock)
            assert producers._inputs_from_current_evidence_on_connection is target
        result = profile.snapshot()
        assert result["text_memo"] is None
        assert not result["incomplete_spans"] and not result["unpaired_submissions"]
        assert len(calls) == (1 if case == "exception" else 2)
        assert result["cpu_capture_valid"] is (case == "first_only")
        if case in {"first_only", "exception"}:
            assert len(result["cpu_profiles"]) == 1
            captured = result["cpu_profiles"][0]
            assert captured["witness"] == witness.identity and captured["clock"] == id(clock)
            assert captured["selected_method"]["qualname"] == target.__qualname__
            entries = captured["entries"]
            assert sum(entry["total_calls"] for entry in entries if entry["function"] == "target") == 1
            assert not {"earlier_work", "later_work"}.intersection(entry["function"] for entry in entries)
            assert captured["within_preparation_window"]
            assert result["cpu_capture_status"] == ("partial_invalid" if case == "exception" else "captured_preparation")
        else:
            assert result["cpu_profiles"] == []
        assert sys.getprofile() is before and sys.monitoring.get_tool(tool) is None
        assert sys.monitoring.get_events(tool) == 0
    finally:
        engine.dispose()
