"""Opt-in original END diagnostic and controls for its timing instrument."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import CodeType

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.continuation_retention_memo import RetentionStageMemo, current_v4_preparation_memo
from elspeth.web.required_executor import RequiredInvocationWitness
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.continuation_end_profile import EndPreparationProfile
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.unit.web.sessions import test_continuation_checkpoint_end_complete_review as original_review

operation_store = original_review.operation_store

PROFILE_FLAG = "ELSPETH_TEST_END_PREPARATION_PROFILE"
PROFILE_PATH = "ELSPETH_TEST_END_PREPARATION_PROFILE_PATH"
CPU_PROFILE_FLAG = "ELSPETH_TEST_END_PREPARATION_CPROFILE"
CPU_SELECTION_FLAG = "ELSPETH_TEST_END_PREPARATION_CPROFILE_SELECTION"
CPU_MONITOR_EVENTS = (
    sys.monitoring.events.PY_START,
    sys.monitoring.events.PY_RESUME,
    sys.monitoring.events.PY_THROW,
    sys.monitoring.events.PY_RETURN,
    sys.monitoring.events.PY_YIELD,
    sys.monitoring.events.PY_UNWIND,
    sys.monitoring.events.CALL,
    sys.monitoring.events.C_RETURN,
    sys.monitoring.events.C_RAISE,
)
# CPython stores CALL as the control bit for both ancillary C-exit events.
# Their callbacks remain separately registered and fire for the original call.
CPU_ANCILLARY_EVENTS = sys.monitoring.events.C_RETURN | sys.monitoring.events.C_RAISE
CPU_ACTIVE_EVENTS = sum(CPU_MONITOR_EVENTS) & ~CPU_ANCILLARY_EVENTS


def _assert_cpu_monitoring_cleanup():
    tool = sys.monitoring.PROFILER_ID
    assert sys.monitoring.get_tool(tool) is None
    assert sys.monitoring.get_events(tool) == 0
    sys.monitoring.use_tool_id(tool, "end-profile-cleanup-control")
    try:
        for monitored_event in CPU_MONITOR_EVENTS:
            assert sys.monitoring.register_callback(tool, monitored_event, None) is None
    finally:
        sys.monitoring.free_tool_id(tool)


@pytest.mark.asyncio
async def test_actual_end_review_preparation_profile(operation_store, tmp_path, monkeypatch, record_property):
    mode = os.environ.get(PROFILE_FLAG)
    if mode is None:
        pytest.skip("opt-in original END diagnostic; set ELSPETH_TEST_END_PREPARATION_PROFILE=0 or 1")
    assert mode in {"0", "1"}
    root = Path(__file__).resolve().parents[4]
    output = Path(os.environ.get(PROFILE_PATH, f".claude/lanes/end-preparation-profile/profile-{mode}.json"))
    if not output.is_absolute():
        output = root / output
    output = output.resolve()
    assert output.is_relative_to(root), "profile output must remain in this project"
    record_property("end_preparation_profile", str(output))
    cpu_mode = os.environ.get(CPU_PROFILE_FLAG, "0")
    assert cpu_mode in {"0", "1"} and (cpu_mode == "0" or mode == "1")
    cpu_selection = os.environ.get(CPU_SELECTION_FLAG, "all_workers")
    profile = EndPreparationProfile(enabled=mode == "1", cpu_profile=cpu_mode == "1", cpu_selection=cpu_selection)
    parents = {}
    proof_children = {}
    original_proof = RetentionStageMemo.canonical_text_proof

    def observe_text_proof(memo, key, producer, digest_producer):
        parent = current_v4_preparation_memo()
        proof = original_proof(memo, key, producer, digest_producer)
        if parent is not None:
            parents[id(parent)] = parent
            if memo is not parent:
                # Keep the immutable proof alive so object-id reuse after an
                # eviction cannot manufacture a cross-child hit in this probe.
                entry = proof_children.setdefault(id(proof), (proof, set()))
                entry[1].add(id(memo))
        return proof

    if mode == "1":
        monkeypatch.setattr(RetentionStageMemo, "canonical_text_proof", observe_text_proof)
    try:
        with profile.install(monkeypatch, operation_store[0]):
            await original_review.test_actual_v4_paid_end_review_retains_blocked_action_without_early_publication(
                operation_store, tmp_path, monkeypatch
            )
    except BaseException as original:
        profile.case_outcome = {"status": "raised", "error": type(original).__name__}
        raise
    else:
        profile.case_outcome = {"status": "returned"}
    finally:
        # A failed output write is recorded separately and cannot replace the
        # original production timeout, cancellation or other test exception.
        failure = profile.flush(output)
        record_property("end_profile_write_error", None if failure is None else type(failure).__name__)
        record_property(
            "end_preparation_text_parent",
            {
                "parents": len(parents),
                "cross_child_proofs": sum(len(children) > 1 for _proof, children in proof_children.values()),
                "parents_closed_empty": all(not parent.active and parent.cached_entry_count == 0 for parent in parents.values()),
            },
        )


@pytest.mark.parametrize("enabled", (False, True), ids=("off", "on"))
@pytest.mark.asyncio
async def test_end_profile_preserves_original_sync_async_results_errors_and_measures_overhead(enabled, record_property):
    profile = EndPreparationProfile(enabled=enabled)
    token = object()
    error = RuntimeError("original instrument control")
    calls = []

    def original(value):
        calls.append(value)
        return value

    async def awaited(value):
        calls.append(value)
        await asyncio.sleep(0)
        return value

    def fails():
        raise error

    async def fails_async():
        raise error

    observed = profile.sync("known-sync", original)
    observed_async = profile.asynchronous("known-async", awaited)
    before = time.monotonic_ns()
    for _ in range(32):
        assert original(token) is token
    baseline_ns = time.monotonic_ns() - before
    before = time.monotonic_ns()
    for _ in range(32):
        assert observed(token) is token
    observed_ns = time.monotonic_ns() - before
    assert await observed_async(token) is token
    with pytest.raises(RuntimeError) as sync_error:
        profile.sync("known-error", fails)()
    assert sync_error.value is error
    with pytest.raises(RuntimeError) as async_error:
        await profile.asynchronous("known-async-error", fails_async)()
    assert async_error.value is error
    assert calls == [token] * 65
    assert baseline_ns > 0 and observed_ns > 0
    record_property("end_profile_enabled", enabled)
    record_property("end_profile_baseline_32_calls_ns", baseline_ns)
    record_property("end_profile_observed_32_calls_ns", observed_ns)
    profile.sync("unused", original)  # Known negative: never invoked.
    snapshot = profile.snapshot()
    assert not snapshot["errors"] and not snapshot["incomplete_spans"]
    if enabled:
        assert len(snapshot["records"]) == 35
        assert {row["label"] for row in snapshot["records"]} == {"known-sync", "known-async", "known-error", "known-async-error"}
        assert all(row["elapsed_ns"] >= 0 and row["thread_cpu_ns"] >= 0 for row in snapshot["records"])
    else:
        assert observed is original and observed_async is awaited and snapshot["records"] == []


def test_end_profile_pairs_actual_sql_success_error_and_removes_listeners():
    profile = EndPreparationProfile(enabled=True)
    engine = create_session_engine("sqlite://")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("select 1")).scalar_one() == 1  # Known unobserved SQL.
            with profile.sql(engine):
                assert conn.execute(text("select 2")).scalar_one() == 2
                with pytest.raises(DBAPIError):
                    conn.execute(text("select from"))
            assert conn.execute(text("select 3")).scalar_one() == 3  # Listeners are gone.
        snapshot = profile.snapshot()
        assert not snapshot["errors"] and not snapshot["incomplete_spans"] and snapshot["pending_sql_contexts"] == 0
        assert len(snapshot["records"]) == 2
        assert snapshot["records"][0]["error"] is None
        assert snapshot["records"][1]["error"] is not None
        assert snapshot["records"][0]["template_sha256"] != snapshot["records"][1]["template_sha256"]
        assert all("statement" not in row and "parameters" not in row for row in snapshot["records"])
    finally:
        engine.dispose()


def test_end_profile_pairs_real_owned_worker_without_replacing_callable_or_witness(monkeypatch):
    profile = EndPreparationProfile(enabled=True)
    token = object()
    error = RuntimeError("original worker control")
    calls = []

    def original():
        calls.append(token)
        return token

    def fails():
        raise error

    with profile.workers(monkeypatch), ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(original).result(timeout=5) is token  # Not an owned witness.
        witness = RequiredInvocationWitness(1)
        future = executor.submit(witness.invoke, original)
        witness.arm()
        assert future.result(timeout=5) is token
        failed = RequiredInvocationWitness(1)
        failure = executor.submit(failed.invoke, fails)
        failed.arm()
        with pytest.raises(RuntimeError) as raised:
            failure.result(timeout=5)
        assert raised.value is error
    assert calls == [token, token]
    assert witness.snapshot().callable_finished and failed.snapshot().callable_finished
    snapshot = profile.snapshot()
    assert not snapshot["errors"] and not snapshot["incomplete_spans"] and not snapshot["unpaired_submissions"]
    submissions = [row for row in snapshot["records"] if row["kind"] == "submission"]
    workers = [row for row in snapshot["records"] if row["kind"] == "worker"]
    assert len(submissions) == len(workers) == 2
    assert [row["future_identity"] for row in submissions] == [id(future), id(failure)]
    assert {row["worker_id"] for row in submissions} == {row["id"] for row in workers}
    assert {row["submission"] for row in workers} == {row["id"] for row in submissions}
    assert all(row["queue_to_worker_ns"] >= 0 for row in workers)


def test_end_profile_flushes_original_failure_without_masking_it(tmp_path):
    profile = EndPreparationProfile(enabled=True)
    original = RuntimeError("original before-finally failure")
    path = tmp_path / "profile.json"
    with pytest.raises(RuntimeError) as raised:
        try:
            with profile.span("failure"):
                raise original
        finally:
            assert profile.flush(path) is None
    assert raised.value is original
    data = json.loads(path.read_text())
    assert data["records"][0]["error"] == "RuntimeError" and data["incomplete_spans"] == []
    assert profile.flush(path / "invalid-child.json") is not None


@pytest.mark.parametrize("existing_hook", (False, True), ids=("capture-original-worker", "refuse-existing-hook"))
def test_end_cpu_profile_tracks_only_original_worker_and_preserves_hooks(monkeypatch, existing_hook):
    profile = EndPreparationProfile(enabled=True, cpu_profile=True)
    # Instrument control only: the real owned witness has no Composer clock.
    # Native END capture selects its actual bound clock at END entry instead.
    profile._end_clocks.add(None)
    marker = object()
    calls = []
    main_hook = sys.getprofile()
    original_tool = sys.monitoring.get_tool(sys.monitoring.PROFILER_ID)
    assert original_tool is None
    untracked_calls = []

    def preserve_hook(_frame, _event, _arg):
        return None

    def initialize():
        if existing_hook:
            sys.setprofile(preserve_hook)

    def original_owned_body():
        calls.append(marker)
        assert len(calls) == 1
        generator = event_generator()
        assert next(generator) is marker
        assert generator.throw(LookupError("original generator throw")) is marker
        with pytest.raises(StopIteration):
            next(generator)
        with pytest.raises(LookupError):
            owned_unwind()
        with pytest.raises(KeyError):
            {}.pop("original C raise")
        peer = threading.Thread(target=concurrent_untracked_body)
        peer.start()
        peer.join()
        return marker

    def untracked_body():
        return marker

    def concurrent_untracked_body():
        untracked_calls.append(marker)

    def event_generator():
        try:
            yield marker
        except LookupError:
            yield marker

    def owned_unwind():
        raise LookupError("original Python unwind")

    with profile.workers(monkeypatch), ThreadPoolExecutor(max_workers=1, initializer=initialize) as executor:
        assert executor.submit(untracked_body).result(timeout=5) is marker
        witness = RequiredInvocationWitness(1)
        actual = executor.submit(witness.invoke, original_owned_body)
        witness.arm()
        assert actual.result(timeout=5) is marker
        assert executor.submit(sys.getprofile).result(timeout=5) is (preserve_hook if existing_hook else None)
        if existing_hook:
            executor.submit(sys.setprofile, None).result(timeout=5)
    assert calls == [marker] and witness.snapshot().callable_finished
    assert untracked_calls == [marker]
    assert sys.getprofile() is main_hook
    assert sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) is original_tool
    _assert_cpu_monitoring_cleanup()
    if existing_hook:
        assert profile._cpu_profiles == []
        assert profile.errors == ["worker CPU profile refused an existing thread hook"]
    else:
        assert not profile.errors and len(profile._cpu_profiles) == 1
        row, captured = profile._cpu_profiles[0]
        assert row["cprofile_timer"] == "time.thread_time_ns" and row["cprofile_timeunit"] == 1e-9
        assert row["cprofile_enclosing_thread_cpu_ns"] > 0
        assert row["cprofile_legacy_hook_before"] is None
        assert row["cprofile_legacy_hook_after_enable"] is None
        assert row["cprofile_legacy_hook_after_disable"] is None
        assert row["cprofile_monitoring_after_enable"] == "cProfile"
        assert row["cprofile_monitoring_after_disable"] is None
        assert row["cprofile_monitoring_events_after_enable"] == CPU_ACTIVE_EVENTS
        assert row["cprofile_monitoring_events_after_disable"] == 0
        entries = captured.getstats()
        names = {entry.code.co_name for entry in entries if isinstance(entry.code, CodeType)}
        assert "original_owned_body" in names and "invoke" in names
        assert "event_generator" in names and "owned_unwind" in names
        assert "untracked_body" not in names
        assert "concurrent_untracked_body" not in names
        assert any(isinstance(entry.code, str) and "builtins.len" in entry.code for entry in entries)


def test_end_cpu_profile_refuses_occupied_monitoring_without_changing_owner_events_or_callbacks(monkeypatch):
    profile = EndPreparationProfile(enabled=True, cpu_profile=True)
    profile._end_clocks.add(None)
    tool = sys.monitoring.PROFILER_ID
    assert sys.monitoring.get_tool(tool) is None
    marker = object()
    calls = []
    observed_events = []

    def original():
        calls.append(marker)
        assert len(calls) == 1
        with pytest.raises(KeyError):
            {}.pop("original occupied-slot C raise")
        return marker

    def callback_for(monitored_event):
        def existing_callback(*args):
            if args[0] is original.__code__:
                observed_events.append(monitored_event)

        return existing_callback

    callbacks = {monitored_event: callback_for(monitored_event) for monitored_event in CPU_MONITOR_EVENTS}
    sys.monitoring.use_tool_id(tool, "original-monitoring-owner")
    try:
        for monitored_event in CPU_MONITOR_EVENTS:
            assert sys.monitoring.register_callback(tool, monitored_event, callbacks[monitored_event]) is None
        mask = sum(CPU_MONITOR_EVENTS)
        sys.monitoring.set_events(tool, mask)
        actual_mask = sys.monitoring.get_events(tool)
        assert actual_mask == mask & ~CPU_ANCILLARY_EVENTS == CPU_ACTIVE_EVENTS
        assert actual_mask & sys.monitoring.events.CALL
        assert actual_mask & CPU_ANCILLARY_EVENTS == 0
        with profile.workers(monkeypatch), ThreadPoolExecutor(max_workers=1) as executor:
            witness = RequiredInvocationWitness(1)
            actual = executor.submit(witness.invoke, original)
            witness.arm()
            assert actual.result(timeout=5) is marker
        assert calls == [marker] and witness.snapshot().callable_finished
        assert profile._cpu_profiles == []
        assert profile.errors == ["worker CPU profile refused an existing monitoring profiler slot"]
        assert sys.monitoring.get_tool(tool) == "original-monitoring-owner"
        assert sys.monitoring.get_events(tool) == actual_mask
        for monitored_event in CPU_MONITOR_EVENTS:
            assert sys.monitoring.register_callback(tool, monitored_event, callbacks[monitored_event]) is callbacks[monitored_event]
        assert sys.monitoring.events.C_RETURN in observed_events
        assert sys.monitoring.events.C_RAISE in observed_events
    finally:
        sys.monitoring.set_events(tool, 0)
        for monitored_event in CPU_MONITOR_EVENTS:
            sys.monitoring.register_callback(tool, monitored_event, None)
        sys.monitoring.free_tool_id(tool)
    _assert_cpu_monitoring_cleanup()


@pytest.mark.asyncio
async def test_end_cpu_profile_exports_failed_before_finish_and_excludes_unrelated_clock(monkeypatch, tmp_path):
    profile = EndPreparationProfile(enabled=True, cpu_profile=True)
    engine = create_session_engine("sqlite://")
    original = RuntimeError("original END entry failed before preparation finish")
    marker = object()
    calls = []

    def original_failed_body():
        calls.append(marker)
        assert len(calls) == 1
        raise original

    def unrelated_body():
        return marker

    try:
        with profile.install(monkeypatch, engine), ThreadPoolExecutor(max_workers=1) as executor:
            policy = minimal_continuation_policy().actions.advisor
            target = ContinuationActionClock(policy)
            unrelated = ContinuationActionClock(policy)
            original_bounds = (target.started_monotonic, target.preparation_deadline_monotonic, target.deadline)
            token = profile._clock.set(id(unrelated))
            try:
                unrelated_witness = RequiredInvocationWitness(1)
                unrelated_future = executor.submit(unrelated_witness.invoke, unrelated_body)
                unrelated_witness.arm()
                assert unrelated_future.result(timeout=5) is marker
            finally:
                profile._clock.reset(token)
            with pytest.raises(RuntimeError) as raised, profile.end_entry(target):
                witness = RequiredInvocationWitness(1)
                future = executor.submit(witness.invoke, original_failed_body)
                witness.arm()
                future.result(timeout=5)
            assert raised.value is original
            assert original_bounds == (target.started_monotonic, target.preparation_deadline_monotonic, target.deadline)
            # This same target clock's post-entry cleanup must also stay out.
            token = profile._clock.set(id(target))
            try:
                cleanup = RequiredInvocationWitness(1)
                cleanup_future = executor.submit(cleanup.invoke, unrelated_body)
                cleanup.arm()
                assert cleanup_future.result(timeout=5) is marker
            finally:
                profile._clock.reset(token)
        assert calls == [marker] and witness.snapshot().callable_finished
        path = tmp_path / "aborted-end-cpu.json"
        assert profile.flush(path) is None
        data = json.loads(path.read_text())
        assert data["cpu_capture_valid"] and data["cpu_capture_status"] == "captured_aborted_entry"
        assert data["cpu_capture_issues"] == data["cpu_missing_workers"] == data["errors"] == []
        assert data["cpu_target_clocks"] == [id(target)]
        assert len(data["cpu_profiles"]) == 1
        captured = data["cpu_profiles"][0]
        assert data["cpu_expected_workers"] == [captured["worker_id"]]
        assert captured["clock"] == id(target) and captured["witness"] == witness.identity
        assert captured["within_preparation_window"] and captured["preparation_finish_ns"] is None
        boundary = captured["capture_boundary"]
        assert boundary["kind"] == "end_entry_aborted" and boundary["error"] == "RuntimeError"
        assert data["records"][boundary["entry_id"]]["end_ns"] == boundary["end_ns"]
        assert not any(row["label"] == "ContinuationActionClock.finish_preparation" for row in data["records"])
        names = {entry["function"] for entry in captured["entries"]}
        assert "original_failed_body" in names and "invoke" in names and "unrelated_body" not in names
        assert any("builtins.len" in entry["function"] for entry in captured["entries"])
        other_workers = [row for row in data["records"] if row["kind"] == "worker" and row["id"] != captured["worker_id"]]
        assert len(other_workers) == 2 and all("cprofile_timer" not in row for row in other_workers)
        _assert_cpu_monitoring_cleanup()
    finally:
        engine.dispose()


@pytest.mark.parametrize("missing", ("target", "boundary"))
@pytest.mark.asyncio
async def test_end_cpu_profile_marks_missing_target_or_boundary_invalid_without_masking_error(monkeypatch, tmp_path, missing):
    profile = EndPreparationProfile(enabled=True, cpu_profile=True)
    engine = create_session_engine("sqlite://")
    original = RuntimeError("original case error remains authoritative")
    try:
        with profile.install(monkeypatch, engine):
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            path = tmp_path / f"missing-{missing}.json"
            with pytest.raises(RuntimeError) as raised:
                try:
                    if missing == "target":
                        raise original
                    with profile.end_entry(clock):
                        # The entry is still active: no finish or exit exists.
                        # A snapshot must expose that missing boundary.
                        assert profile.flush(path) is None
                        raise original
                finally:
                    if missing == "target":
                        assert profile.flush(path) is None
            assert raised.value is original
        data = json.loads(path.read_text())
        assert not data["cpu_capture_valid"] and data["cpu_capture_status"] == "invalid"
        expected = (
            "CPU capture has no original END clock target"
            if missing == "target"
            else f"CPU target {id(clock)} has no preparation-finish or END-entry-exit boundary"
        )
        assert expected in data["cpu_capture_issues"]
        assert data["cpu_profiles"] == []
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_end_cpu_profile_selects_original_checkpoint_method_and_preserves_failed_worker(operation_store, monkeypatch):
    profile = EndPreparationProfile(enabled=True, cpu_profile=True, cpu_selection="checkpoint_source")
    service = operation_store[3]
    assert type(service) is SessionServiceImpl
    original_method = SessionServiceImpl.prepare_v4_checkpoint_source_sync
    argument = object()  # The real nominal-input guard is this instrument control's failure.
    errors = []
    calls = []

    def selected_original_body():
        calls.append(argument)
        try:
            return service.prepare_v4_checkpoint_source_sync(prepared=argument)
        except AuditIntegrityError as original:
            errors.append(original)
            raise

    def unrelated_body():
        return argument

    with profile.install(monkeypatch, operation_store[0]), ThreadPoolExecutor(max_workers=1) as executor:
        clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
        with pytest.raises(AuditIntegrityError) as raised, profile.end_entry(clock):
            unrelated = RequiredInvocationWitness(1)
            unrelated_future = executor.submit(unrelated.invoke, unrelated_body)
            unrelated.arm()
            assert unrelated_future.result(timeout=5) is argument
            witness = RequiredInvocationWitness(1)
            future = executor.submit(witness.invoke, selected_original_body)
            witness.arm()
            future.result(timeout=5)
    assert calls == [argument] and errors == [raised.value]
    assert str(raised.value) == "checkpoint source preparation requires its original nominal producer"
    assert witness.snapshot().callable_finished and unrelated.snapshot().callable_finished
    data = profile.snapshot()
    assert data["cpu_selection"] == "checkpoint_source"
    assert data["cpu_capture_valid"] and data["cpu_capture_status"] == "captured_aborted_entry"
    assert not data["cpu_capture_issues"] and not data["cpu_missing_workers"]
    assert len(data["cpu_profiles"]) == 1
    captured = data["cpu_profiles"][0]
    assert captured["witness"] == witness.identity and captured["clock"] == id(clock)
    assert captured["preparation_finish_ns"] is None and captured["capture_boundary"]["error"] == "AuditIntegrityError"
    assert captured["selected_method"] == {
        "module": original_method.__module__,
        "qualname": original_method.__qualname__,
        "file": original_method.__code__.co_filename,
        "line": original_method.__code__.co_firstlineno,
    }
    assert "gate excluded" in captured["scope"]
    assert data["records"][captured["selected_method_span"]]["error"] == "AuditIntegrityError"
    entries = captured["entries"]
    assert any(
        entry["file"] == original_method.__code__.co_filename
        and entry["line"] == original_method.__code__.co_firstlineno
        and entry["function"] == original_method.__code__.co_name
        for entry in entries
    )
    assert not any(entry["function"] in {"unrelated_body", "selected_original_body", "invoke"} for entry in entries)
    _assert_cpu_monitoring_cleanup()


@pytest.mark.asyncio
async def test_end_cpu_profile_selected_method_not_reached_is_invalid(monkeypatch):
    profile = EndPreparationProfile(enabled=True, cpu_profile=True, cpu_selection="checkpoint_source")
    engine = create_session_engine("sqlite://")
    original = RuntimeError("unrelated original worker")

    def unrelated_body():
        raise original

    try:
        with profile.install(monkeypatch, engine), ThreadPoolExecutor(max_workers=1) as executor:
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            with pytest.raises(RuntimeError) as raised, profile.end_entry(clock):
                witness = RequiredInvocationWitness(1)
                future = executor.submit(witness.invoke, unrelated_body)
                witness.arm()
                future.result(timeout=5)
            assert raised.value is original and witness.snapshot().callable_finished
        data = profile.snapshot()
        assert not data["cpu_capture_valid"] and data["cpu_capture_status"] == "invalid"
        assert data["cpu_profiles"] == data["cpu_expected_workers"] == []
        assert "CPU capture did not reach the original selected checkpoint-source method" in data["cpu_capture_issues"]
        _assert_cpu_monitoring_cleanup()
    finally:
        engine.dispose()
