"""Opt-in original END diagnostic and controls for its timing instrument."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import CodeType, SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import continuation_retention_memo as memo_module
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.continuation_retention_memo import (
    CanonicalTextEncodingKey,
    CanonicalTextVerificationKey,
    RecorderBlockHashKey,
    RecorderRequestTextDependencies,
    RecorderRequestTextKey,
    RecorderRequestTextProof,
    retention_stage_memo_scope,
    v4_preparation_memo_scope,
)
from elspeth.web.required_executor import RequiredInvocationAborted, RequiredInvocationWitness
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.continuation_end_profile import (
    EndPreparationProfile,
    EndTextMemoObservation,
    StopDecisionProfile,
    _driver_review_observer,
    _driver_stop_observer,
    driver_end_preparation_profile,
)
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.unit.web.sessions import test_continuation_checkpoint_end_complete_review as original_review

operation_store = original_review.operation_store

PROFILE_FLAG = "ELSPETH_TEST_END_PREPARATION_PROFILE"
PROFILE_PATH = "ELSPETH_TEST_END_PREPARATION_PROFILE_PATH"
CPU_PROFILE_FLAG = "ELSPETH_TEST_END_PREPARATION_CPROFILE"
CPU_SELECTION_FLAG = "ELSPETH_TEST_END_PREPARATION_CPROFILE_SELECTION"
TEXT_MEMO_BUDGET_FLAG = "ELSPETH_TEST_END_PREPARATION_TEXT_MEMO_MIB"
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


def test_driver_end_profile_absent_flag_preserves_original_alias(monkeypatch):
    from elspeth.web.sessions import continuation_driver as driver

    monkeypatch.delenv("ELSPETH_TEST_DRIVER_END_PROFILE", raising=False)
    original = driver.review_prescribed_continuation_end
    original_semantic = driver.run_continuation_semantic_advisor
    with driver_end_preparation_profile(monkeypatch, None, session_id="session", operation_id="operation"):
        assert driver.review_prescribed_continuation_end is original
        assert driver.run_continuation_semantic_advisor is original_semantic
    assert driver.review_prescribed_continuation_end is original


def test_driver_end_profile_output_error_cannot_mask_original(monkeypatch, capsys):
    from elspeth.web.sessions import continuation_driver as driver

    monkeypatch.setenv("ELSPETH_TEST_DRIVER_END_PROFILE", "spans")
    monkeypatch.setenv("ELSPETH_TEST_DRIVER_END_PROFILE_PATH", str(Path(__file__).resolve().parents[4] / ".scratch/end-control.json"))
    original = driver.review_prescribed_continuation_end
    error = RuntimeError("original driver failure")
    outcomes = []

    def failed_output(profile, path):
        outcomes.append(profile.case_outcome)
        raise ValueError("diagnostic serialization failure")

    # Isolate output-error handling; real worker/SQL installation has separate controls.
    monkeypatch.setattr(EndPreparationProfile, "install", lambda *_args: nullcontext())
    monkeypatch.setattr(EndPreparationProfile, "flush", failed_output)
    with (
        pytest.raises(RuntimeError) as raised,
        driver_end_preparation_profile(monkeypatch, None, session_id="session", operation_id="operation"),
    ):
        assert driver.review_prescribed_continuation_end is not original
        raise error
    assert raised.value is error and driver.review_prescribed_continuation_end is original
    assert outcomes == [{"status": "raised", "error": "RuntimeError"}]
    assert "END driver profile write failed: ValueError" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", (False, True))
async def test_driver_end_adapter_selects_original_cut_and_forwards_identity(failure):
    from elspeth.web.composer.continuation import EndFrame, PrivateChunkRef

    # Adapter metadata controls only; these are not issued SQL/runtime owners.
    reference = PrivateChunkRef(codec="composer.state.v1", digest="a" * 64, byte_length=2)
    frame = EndFrame(phase="review_needed", state=reference, candidate=reference, publication_binding=None)
    logical = SimpleNamespace(session_id="session", operation_id="operation")
    root = SimpleNamespace(logical=logical, frames=(frame,), cursor=29)
    selected = SimpleNamespace(stack=root)
    clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
    phases = SimpleNamespace(action_clock=clock)
    services, result = object(), object()
    original_error = LookupError("original review failure")
    calls = []
    profile = EndPreparationProfile(enabled=True)

    async def original(actual_phases, actual_services, actual_selected):
        assert actual_phases is phases and actual_services is services and actual_selected is selected
        calls.append(profile._clock.get())
        if failure:
            raise original_error
        return result

    observed = _driver_review_observer(profile, original, session_id="session", operation_id="operation")

    async def invoke():
        if failure:
            with pytest.raises(LookupError) as raised:
                await observed(phases, services, selected)
            assert raised.value is original_error
        else:
            assert await observed(phases, services, selected) is result

    logical.session_id = "foreign-session"
    await invoke()
    logical.session_id = "session"
    logical.operation_id = "foreign-operation"
    await invoke()
    logical.operation_id = "operation"
    root.frames = (frame.model_copy(update={"phase": "repair_needed"}),)
    await invoke()
    root.frames = (object(),)
    await invoke()
    root.frames = (frame,)
    phases.action_clock = None
    await invoke()
    phases.action_clock = clock
    assert not profile._end_clocks and calls == [None] * 5
    bounds = clock.started_monotonic, clock.preparation_deadline_monotonic, clock.deadline
    await invoke()
    assert calls[-1] == id(clock) and profile._end_clocks == {id(clock)}
    assert bounds == (clock.started_monotonic, clock.preparation_deadline_monotonic, clock.deadline) and not clock._seen
    entry = next(row for row in profile.records if row["label"] == "driver.original_review_entry")
    assert entry["entry_lag_ns"] >= 0 and entry["original_start_ns"] == int(clock.started_monotonic * 1_000_000_000)
    assert entry["cursor"] == 29 and entry["error"] == ("LookupError" if failure else None)
    await invoke()
    assert calls[-1] is None  # Only one original Review is selected.


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
    default_budget_mib = memo_module.CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES // 1_048_576
    budget_mib = os.environ.get(TEXT_MEMO_BUDGET_FLAG, str(default_budget_mib))
    assert budget_mib in {"1", "5", "6", str(default_budget_mib)}
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", int(budget_mib) * 1_048_576)
    record_property("end_preparation_text_memo_budget_mib", int(budget_mib))
    profile.text_memo_budget_bytes = int(budget_mib) * 1_048_576
    profile.text_memo = EndTextMemoObservation() if mode == "1" else None
    try:
        with (
            profile.install(monkeypatch, operation_store[0]),
            profile.text_memo.install(monkeypatch) if profile.text_memo is not None else nullcontext(),
        ):
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
        record_property("end_preparation_text_memo", None if profile.text_memo is None else profile.text_memo.snapshot())


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_mib", (1, 5))
async def test_text_memo_observer_calibrates_hit_miss_eviction_error_closed_and_ordinary_negative(monkeypatch, budget_mib):
    from elspeth.contracts.credential_material import _classify_string
    from tests.unit.web.test_continuation_retention_memo import (
        _admit_uncached_recorder_data,
        _evidence_key,
        _produce_uncached_recorder_data,
        _recorder_data_chunk,
    )

    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", budget_mib * 1_048_576)
    observer = EndTextMemoObservation()
    calls = []
    original_error = RuntimeError("original canonical producer failure")
    first = CanonicalTextVerificationKey("sensitive-observer-control" + "a" * 600_000, 1_048_576)
    second = CanonicalTextVerificationKey("b" * 600_000, 1_048_576)
    evidence_key = _evidence_key()
    recorder_chunk = _recorder_data_chunk()
    recorder_key = memo_module.PrivateChunkVerificationKey(recorder_chunk.reference, recorder_chunk.canonical)

    def verify(text):
        calls.append(len(text))

    def fails(_text):
        raise original_error

    def encode(key):
        return key.canonical.encode()

    def block(_key):
        pass

    def request(_key):
        return RecorderRequestTextProof("{}", ())

    def evidence(_text):
        return (evidence_key.reference,)

    with observer.install(monkeypatch):
        with retention_stage_memo_scope() as ordinary:
            ordinary.canonical_text_verification(first, verify)
        assert observer.snapshot()["families"]["canonical"]["requests"] == 0
        clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
        with v4_preparation_memo_scope(clock) as parent:
            for key in (first, second, first):
                parent.canonical_text_verification(key, verify)
            full_key = CanonicalTextEncodingKey("full", 100)
            block_key = RecorderBlockHashKey("0" * 64, "block")
            for _ in range(2):
                parent.canonical_text_proof(full_key, encode, hashlib.sha256)
                parent.recorder_block_hash(block_key, block)
                parent.v4_recorder_request_text(
                    # The retained request includes dependency/reverse-index
                    # metadata. A 100-byte allocation cannot admit that entry.
                    RecorderRequestTextKey("request", 4096),
                    RecorderRequestTextDependencies(encode, encode, hashlib.sha256, frozenset({"shape"})),
                    request,
                )
                assert parent.credential_string_category("ordinary calibration text", _classify_string) is None
                parent.evidence_references(evidence_key, evidence)
                parent.recorder_data(recorder_key, _produce_uncached_recorder_data, _admit_uncached_recorder_data)
            with pytest.raises(RuntimeError) as raised:
                parent.canonical_text_verification(CanonicalTextVerificationKey("fails", 100), fails)
            assert raised.value is original_error
        parent.canonical_text_verification(first, verify)  # Closed work recomputes without publication.
    snapshot = observer.snapshot()
    canonical = snapshot["families"]["canonical"]
    assert canonical["requests"] == 5 and canonical["errors"] == 1 and canonical["unretained"] == 1
    assert canonical["hits"] == (0 if budget_mib == 1 else 1)
    assert canonical["evictions"] == (2 if budget_mib == 1 else 0)
    assert canonical["recomputations"] == (2 if budget_mib == 1 else 1)
    assert len(calls) == (5 if budget_mib == 1 else 4)
    fields = ("requests", "misses", "hits", "errors", "admissions", "evictions", "recomputations", "unretained")
    expected = {
        "canonical": (
            5,
            5 if budget_mib == 1 else 4,
            0 if budget_mib == 1 else 1,
            1,
            3 if budget_mib == 1 else 2,
            2 if budget_mib == 1 else 0,
            2 if budget_mib == 1 else 1,
            1,
        ),
        "full": (2, 1, 1, 0, 1, 0, 0, 0),
        "block": (2, 1, 1, 0, 1, 0, 0, 0),
        "request": (2, 1, 1, 0, 1, 0, 0, 0),
        "credential": (2, 1, 1, 0, 1, 0, 0, 0),
        "evidence": (2, 1, 1, 0, 1, 0, 0, 0),
        "recorder": (2, 1, 1, 0, 1, 0, 0, 0),
    }

    def require_exact_events(families):
        assert {family: tuple(counts[field] for field in fields) for family, counts in families.items()} == expected

    require_exact_events(snapshot["families"])
    omitted = {family: counts for family, counts in snapshot["families"].items() if family != "recorder"}
    with pytest.raises(AssertionError):
        require_exact_events(omitted)
    drifted = {family: dict(counts) for family, counts in snapshot["families"].items()}
    drifted["request"]["hits"] += 1
    with pytest.raises(AssertionError):
        require_exact_events(drifted)
    assert snapshot["parents"] == [{"budget": budget_mib * 1_048_576, "closed_empty": True}]
    assert snapshot["observer_errors"] == snapshot["overlapping_calls"] == snapshot["budget_violations"] == 0
    assert snapshot["peak_retained_bytes"] <= budget_mib * 1_048_576 and snapshot["observer_ns"] > 0
    assert snapshot["observed_key_weight_estimate"] > 1_048_576
    assert "sensitive-observer-control" not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_text_memo_observer_censors_working_set_after_fingerprint_capacity(monkeypatch):
    observer = EndTextMemoObservation(max_fingerprints=1)

    def verify(_text):
        pass

    with observer.install(monkeypatch):
        clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
        with v4_preparation_memo_scope(clock) as parent:
            for text_value in ("first", "second", "first"):
                parent.canonical_text_verification(CanonicalTextVerificationKey(text_value, 100), verify)
    snapshot = observer.snapshot()
    assert snapshot["fingerprints"] == snapshot["fingerprint_capacity"] == 1
    assert snapshot["fingerprint_drops"] == 2 and snapshot["observed_key_weight_estimate"] is None
    assert snapshot["families"]["canonical"]["hits"] == 1
    assert snapshot["observer_errors"] == snapshot["budget_violations"] == 0


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


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", (False, True), ids=("returned", "aborted-entry"))
@pytest.mark.parametrize("existing_hook", (False, True), ids=("capture", "retain-existing-hook"))
async def test_first_capture_cpu_selects_original_scope_once_and_preserves_worker_controls(monkeypatch, failed, existing_hook):
    from elspeth.web.composer import continuation_runtime as runtime

    profile = EndPreparationProfile(enabled=True, cpu_profile=True, cpu_selection="first_capture")
    engine = create_session_engine("sqlite://")
    marker = object()
    original_error = RuntimeError("original first capture error")
    original_scope = runtime.borrow_v4_preparation_memo_scope
    original_invoke = RequiredInvocationWitness.invoke
    received = []
    expected = []
    scopes = []
    calls = []
    thread_calls = []

    @contextmanager
    def counted_original_scope(*args, **kwargs):
        scopes.append("enter")
        try:
            with original_scope(*args, **kwargs) as memo:
                yield memo
        finally:
            scopes.append("exit")

    def tracked_invoke(witness, callable_):
        received.append((witness, callable_))
        return original_invoke(witness, callable_)

    def preserve_hook(_frame, _event, _arg):
        pass

    def initialize():
        if existing_hook:
            sys.setprofile(preserve_hook)

    def authority_read_control():
        calls.append("authority")

    def capture_owners_control():
        calls.append("capture")

    def unrelated_thread_control():
        thread_calls.append(marker)

    def first_capture_worker():
        with runtime.borrow_v4_preparation_memo_scope():
            authority_read_control()
            capture_owners_control()
            authority_read_control()
            peer = threading.Thread(target=unrelated_thread_control)
            peer.start()
            peer.join()
            if failed:
                raise original_error
            return marker

    def other_capture_worker():
        with runtime.borrow_v4_preparation_memo_scope():
            return marker

    def submit(executor, callable_, *, aborted=False):
        witness = RequiredInvocationWitness(1)
        expected.append((witness, callable_))
        future = executor.submit(witness.invoke, callable_)
        witness.abort() if aborted else witness.arm()
        return witness, future

    monkeypatch.setattr(runtime, "borrow_v4_preparation_memo_scope", counted_original_scope)
    monkeypatch.setattr(RequiredInvocationWitness, "invoke", tracked_invoke)
    try:
        with profile.install(monkeypatch, engine), ThreadPoolExecutor(max_workers=1, initializer=initialize) as executor:
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            foreign_clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            with pytest.raises(RuntimeError) if failed else nullcontext() as raised, profile.end_entry(clock):
                with profile.span("earlier._retain"):
                    unrelated, future = submit(executor, other_capture_worker)
                    assert future.result(timeout=5) is marker and unrelated.snapshot().callable_finished
                token = profile._clock.set(id(foreign_clock))
                try:
                    with profile.span(profile.FIRST_CAPTURE_PARENT):
                        foreign, future = submit(executor, other_capture_worker)
                        assert future.result(timeout=5) is marker and foreign.snapshot().callable_finished
                finally:
                    profile._clock.reset(token)
                with profile.span(profile.FIRST_CAPTURE_PARENT):
                    aborted, future = submit(executor, other_capture_worker, aborted=True)
                    with pytest.raises(RequiredInvocationAborted):
                        future.result(timeout=5)
                    assert aborted.snapshot().valid_aborted_exit
                    selected, future = submit(executor, first_capture_worker)
                    if failed:
                        with pytest.raises(RuntimeError) as worker_failure:
                            future.result(timeout=5)
                        assert worker_failure.value is original_error
                    else:
                        assert future.result(timeout=5) is marker
                    second, future = submit(executor, other_capture_worker)
                    assert future.result(timeout=5) is marker and second.snapshot().callable_finished
                assert executor.submit(sys.getprofile).result(timeout=5) is (preserve_hook if existing_hook else None)
                if failed:
                    raise original_error
                clock.finish_preparation()
            if failed:
                assert raised.value is original_error
            assert selected.snapshot().callable_finished
        assert received == expected and scopes == ["enter", "exit"] * 4
        assert calls == ["authority", "capture", "authority"] and thread_calls == [marker]
        data = profile.snapshot()
        selected_rows = [row for row in data["records"] if "cprofile_selected_method" in row]
        assert len(selected_rows) == 1
        assert data["records"][selected_rows[0]["submission"]]["witness"] == selected.identity
        if existing_hook:
            assert not data["cpu_capture_valid"] and not data["cpu_profiles"]
            assert selected_rows[0]["cprofile_refused"] == "existing thread profile hook"
        else:
            assert data["cpu_capture_valid"] and len(data["cpu_profiles"]) == 1
            captured = data["cpu_profiles"][0]
            assert captured["witness"] == selected.identity and captured["clock"] == id(clock)
            assert "initial capture Python frame entry excluded" in captured["scope"]
            assert "queue and gate excluded" in captured["scope"]
            assert captured["selected_method"]["qualname"] == "capture_continuation_runtime_data.<locals>.capture"
            names = {entry["function"]: entry for entry in captured["entries"]}
            assert names["authority_read_control"]["total_calls"] == 2
            assert names["capture_owners_control"]["total_calls"] == 1
            assert not {"other_capture_worker", "unrelated_thread_control", "invoke"} & names.keys()
            assert data["cpu_capture_status"] == ("captured_aborted_entry" if failed else "captured_preparation")
        _assert_cpu_monitoring_cleanup()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_first_capture_cpu_not_reached_keeps_original_error_and_invalid_receipt(monkeypatch):
    profile = EndPreparationProfile(enabled=True, cpu_profile=True, cpu_selection="first_capture")
    engine = create_session_engine("sqlite://")
    original = RuntimeError("first capture was never reached")
    try:
        with profile.install(monkeypatch, engine):
            clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
            with pytest.raises(RuntimeError) as raised, profile.end_entry(clock):
                raise original
        assert raised.value is original
        data = profile.snapshot()
        assert not data["cpu_capture_valid"] and not data["cpu_profiles"]
        assert "CPU capture did not reach the original first physical capture scope" in data["cpu_capture_issues"]
        _assert_cpu_monitoring_cleanup()
    finally:
        engine.dispose()


def _stop_profile_metadata():
    from elspeth.web.composer.continuation import AdvisorFrame, EndFrame, PrivateChunkRef

    # Discriminator-only metadata, never passed to a production authority.
    reference = PrivateChunkRef(codec="composer.state.v1", digest="a" * 64, byte_length=2)
    end = EndFrame(phase="publication_ready", state=reference, candidate=reference, publication_binding="binding")
    advisor = AdvisorFrame(phase="response_retained", review=reference, response=reference, coverage_binding="coverage")
    root = SimpleNamespace(
        logical=SimpleNamespace(session_id="session", operation_id="operation"), frames=(object(), end, advisor), cursor=27
    )
    clock = ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    return SimpleNamespace(action_clock=clock), SimpleNamespace(stack=root)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ("session", "operation", "cursor", "parent", "phase", "clock", "used_clock"))
async def test_stop_profile_target_discrimination_and_single_entry(wrong):
    phases, selected = _stop_profile_metadata()
    profile = StopDecisionProfile(enabled=True, cpu_profile=True)
    actual_clock = phases.action_clock
    actual_frames = selected.stack.frames
    result = object()
    seen = []

    async def original(actual_phases, actual_selected):
        assert actual_phases is phases and actual_selected is selected
        seen.append(profile._clock.get())
        return result

    observed = _driver_stop_observer(profile, original, session_id="session", operation_id="operation", cursor=27)
    if wrong == "session":
        selected.stack.logical.session_id = "other"
    elif wrong == "operation":
        selected.stack.logical.operation_id = "other"
    elif wrong == "cursor":
        selected.stack.cursor = 28
    elif wrong == "parent":
        selected.stack.frames = (actual_frames[0], actual_frames[1].model_copy(update={"phase": "review_needed"}), actual_frames[2])
    elif wrong == "phase":
        selected.stack.frames = (*actual_frames[:2], actual_frames[2].model_copy(update={"phase": "decision_ready"}))
    elif wrong == "clock":
        phases.action_clock = None
    else:
        actual_clock._seen.add("preparation")
    assert await observed(phases, selected) is result
    assert not profile._end_clocks and seen == [None]
    selected.stack.logical.session_id = "session"
    selected.stack.logical.operation_id = "operation"
    selected.stack.cursor = 27
    selected.stack.frames = actual_frames
    phases.action_clock = actual_clock
    actual_clock._seen.clear()
    bounds = actual_clock.started_monotonic, actual_clock.preparation_deadline_monotonic, actual_clock.deadline
    assert await observed(phases, selected) is result
    assert seen[-1] == id(actual_clock)
    assert await observed(phases, selected) is result
    assert seen[-1] is None
    assert bounds == (actual_clock.started_monotonic, actual_clock.preparation_deadline_monotonic, actual_clock.deadline)
    assert not actual_clock._seen
    assert not profile.snapshot()["cpu_capture_valid"]  # Selection alone is not worker evidence.


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("none", "error", "cancel", "deferred_group", "abort"))
async def test_stop_profile_actual_worker_context_cleanup_and_original_failures(monkeypatch, failure):
    from elspeth.web.composer import continuation_semantic_advisor_phase as phase
    from elspeth.web.continuation_retention_memo import prepare_retention_sql_callable

    phases, selected = _stop_profile_metadata()
    profile = StopDecisionProfile(enabled=True, cpu_profile=True)
    engine = create_session_engine("sqlite://")
    marker = object()
    error = LookupError("original SQL failure")
    cancel = asyncio.CancelledError("original deferred cancellation")
    original_failure = (
        BaseExceptionGroup("original SQL/deferred cancellation", [error, cancel])
        if failure == "deferred_group"
        else cancel
        if failure == "cancel"
        else error
    )
    original_install = phase.install_current_semantic_decision_on_connection
    original_consume = phase.consume_semantic_advisor_end_stop_on_connection
    calls = []
    transported = []
    original_invoke = RequiredInvocationWitness.invoke
    main_hook = sys.getprofile()

    def witness_observer(witness, callable_):
        transported.append((witness, callable_))
        return original_invoke(witness, callable_)

    def body():
        # The real retention adapter resets ContextVars; native worker lookup
        # must still associate SQL/call spans with the original submitted clock.
        assert profile._clock.get() is None
        calls.append(marker)
        with profile.span("control.original_sql_body"):
            with engine.connect() as conn:
                assert conn.execute(text("SELECT 1")).scalar_one() == 1
            if failure not in ("none", "abort"):
                raise original_failure
            return marker

    monkeypatch.setattr(RequiredInvocationWitness, "invoke", witness_observer)
    with profile.install(monkeypatch, engine), ThreadPoolExecutor(max_workers=1) as executor:

        async def original(actual_phases, actual_selected):
            assert actual_phases is phases and actual_selected is selected
            with retention_stage_memo_scope():
                callable_ = prepare_retention_sql_callable(body)
                witness = RequiredInvocationWitness(1)
                future = executor.submit(witness.invoke, callable_)
                witness.abort() if failure == "abort" else witness.arm()
                try:
                    return future.result(timeout=10)
                finally:
                    assert transported[-1] == (witness, callable_)
                    assert executor.submit(sys.getprofile).result(timeout=5) is None

        observed = _driver_stop_observer(profile, original, session_id="session", operation_id="operation", cursor=27)
        if failure == "none":
            assert await observed(phases, selected) is marker
        elif failure == "abort":
            with pytest.raises(RequiredInvocationAborted):
                await observed(phases, selected)
        else:
            with pytest.raises(type(original_failure)) as raised:
                await observed(phases, selected)
            assert raised.value is original_failure
        assert executor.submit(lambda: marker).result(timeout=5) is marker
    assert phase.install_current_semantic_decision_on_connection is original_install
    assert phase.consume_semantic_advisor_end_stop_on_connection is original_consume
    assert calls == ([] if failure == "abort" else [marker])
    assert sys.getprofile() is main_hook
    _assert_cpu_monitoring_cleanup()
    snapshot = profile.snapshot()
    assert snapshot["cpu_capture_valid"], snapshot["cpu_capture_issues"]
    assert snapshot["cpu_capture_status"] == ("captured_sql_action" if failure == "none" else "captured_sql_action_aborted")
    assert len(snapshot["cpu_profiles"]) == len(snapshot["cpu_expected_workers"]) == 1
    assert snapshot["cpu_profiles"][0]["within_sql_action_window"]
    functions = {entry["function"] for entry in snapshot["cpu_profiles"][0]["entries"]}
    assert ("body" in functions) is (failure != "abort")
    body_spans = [row for row in snapshot["records"] if row["label"] == "control.original_sql_body"]
    assert len(body_spans) == (0 if failure == "abort" else 1)
    assert all(row["clock"] == id(phases.action_clock) and row["worker"] is not None for row in body_spans)
    assert not snapshot["errors"] and not snapshot["incomplete_spans"] and not snapshot["unpaired_submissions"]
    assert snapshot["pending_sql_contexts"] == 0
    assert not phases.action_clock._seen
    engine.dispose()


@pytest.mark.asyncio
async def test_stop_profile_preserves_actual_required_sql_join_after_task_cancellation(monkeypatch):
    from elspeth.web import async_workers
    from elspeth.web.required_sql_outcomes import RequiredSQLReturned
    from tests.unit.web.test_required_sql_finish_once import publication_ticket

    phases, selected = _stop_profile_metadata()
    profile = StopDecisionProfile(enabled=True, cpu_profile=True)
    engine = create_session_engine("sqlite://")
    ticket = publication_ticket()
    entered, release = threading.Event(), threading.Event()
    value = object()
    cancellations = []
    original_retain = async_workers._retain_cancellation

    def retain(actual, cancellation):
        cancellations.append(cancellation)
        original_retain(actual, cancellation)

    def sql():
        entered.set()
        assert release.wait(timeout=10)
        return value

    async def original(actual_phases, actual_selected):
        assert actual_phases is phases and actual_selected is selected
        return await async_workers.run_required_sql_finish_once(ticket, sql)

    monkeypatch.setattr(async_workers, "_retain_cancellation", retain)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", ThreadPoolExecutor(max_workers=1))
    observed = _driver_stop_observer(profile, original, session_id="session", operation_id="operation", cursor=27)
    try:
        with profile.install(monkeypatch, engine):
            task = asyncio.create_task(observed(phases, selected))
            try:
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        await asyncio.sleep(0.001)
                    task.cancel("original Stop SQL deadline control")
                    while not cancellations:
                        await asyncio.sleep(0.001)
                assert not task.done() and not ticket.complete
                release.set()
                outcome = await task
                assert type(outcome) is RequiredSQLReturned and outcome.value is value
                assert outcome.deferred_cancellations == tuple(cancellations)
                assert len(cancellations) == 1 and outcome.deferred_cancellations[0] is cancellations[0]
                assert ticket.complete
            finally:
                release.set()
                if not task.done():
                    await task
        snapshot = profile.snapshot()
        assert snapshot["cpu_capture_valid"], snapshot["cpu_capture_issues"]
        assert snapshot["cpu_capture_status"] == "captured_sql_action"
        assert len(snapshot["cpu_profiles"]) == 1
        assert not snapshot["incomplete_spans"] and not snapshot["unpaired_submissions"]
        _assert_cpu_monitoring_cleanup()
    finally:
        release.set()
        await async_workers.shutdown_async_workers()
        engine.dispose()


@pytest.mark.asyncio
async def test_stop_profile_opt_in_cursor_override_and_driver_alias_restoration(monkeypatch):
    from elspeth.web.sessions import continuation_driver as driver

    phases, selected = _stop_profile_metadata()
    snapshots = []
    marker = object()

    async def original(actual_phases, actual_selected):
        assert actual_phases is phases and actual_selected is selected
        return marker

    def capture(profile, _path):
        snapshots.append(profile.snapshot())
        return None

    monkeypatch.setattr(driver, "run_continuation_semantic_advisor", original)
    monkeypatch.setattr(StopDecisionProfile, "flush", capture)
    monkeypatch.setenv("ELSPETH_TEST_DRIVER_END_PROFILE", "stop_cpu")
    monkeypatch.setenv("ELSPETH_TEST_DRIVER_STOP_CURSOR", "17")
    monkeypatch.setenv("ELSPETH_TEST_DRIVER_END_PROFILE_PATH", ".scratch/stop-adapter-control.json")
    review = driver.review_prescribed_continuation_end
    engine = create_session_engine("sqlite://")
    try:
        with driver_end_preparation_profile(monkeypatch, engine, session_id="session", operation_id="operation"):
            assert driver.review_prescribed_continuation_end is review
            assert await driver.run_continuation_semantic_advisor(phases, selected) is marker
            selected.stack.cursor = 17
            assert await driver.run_continuation_semantic_advisor(phases, selected) is marker
        assert driver.run_continuation_semantic_advisor is original
        assert len(snapshots) == 1
        entries = [row for row in snapshots[0]["records"] if row["label"] == "driver.original_stop_decision"]
        assert len(entries) == 1 and entries[0]["cursor"] == 17
        assert not snapshots[0]["cpu_capture_valid"]  # No actual worker was submitted in this adapter control.
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", (False, True))
async def test_review_memo_observer_selects_exact_cursor_clock_and_worker_interval(monkeypatch, failure):
    from elspeth.web.continuation_retention_memo import borrow_v4_preparation_memo_scope, prepare_retention_sql_callable

    phases, selected = _stop_profile_metadata()
    selected.stack.frames = (selected.stack.frames[0], selected.stack.frames[1].model_copy(update={"phase": "review_needed"}))
    selected.stack.cursor = 19
    profile = EndPreparationProfile(enabled=True)
    profile.text_memo = EndTextMemoObservation(include=profile.include_review_memo)
    original_error = LookupError("original Review failure")
    key = CanonicalTextVerificationKey("original-sensitive-reference-control", 1024)
    produced = []

    def verify(actual):
        assert actual is key.canonical
        produced.append(1)

    def worker():
        assert profile._clock.get() is None  # Actual empty-Context transport.
        with borrow_v4_preparation_memo_scope() as child:
            child.canonical_text_verification(key, verify)
            child.canonical_text_verification(key, verify)

    engine = create_session_engine("sqlite://")
    postfinish = []

    def after_finish(_key):
        postfinish.append(1)

    with profile.install(monkeypatch, engine), profile.text_memo.install(monkeypatch), ThreadPoolExecutor(max_workers=1) as executor:

        async def original(actual_phases, actual_services, actual_selected):
            assert actual_phases is phases and actual_services is services and actual_selected is selected
            if selected.stack.cursor == 19:
                foreign_clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)

                def unrelated_review():
                    with v4_preparation_memo_scope(foreign_clock) as foreign:
                        foreign.canonical_text_verification(key, verify)

                executor.submit(unrelated_review).result(timeout=5)
            with v4_preparation_memo_scope(phases.action_clock) as parent:
                callable_ = prepare_retention_sql_callable(worker)
                witness = RequiredInvocationWitness(1)
                future = executor.submit(witness.invoke, callable_)
                witness.arm()
                future.result(timeout=5)
                if selected.stack.cursor == 19 and not failure:
                    phases.action_clock.finish_preparation()
                    parent.canonical_text_verification(CanonicalTextVerificationKey("postfinish", 1024), after_finish)
            if failure and selected.stack.cursor == 19:
                raise original_error
            return services

        services = object()
        observed = _driver_review_observer(profile, original, session_id="session", operation_id="operation", cursor=19)
        selected.stack.cursor = 18
        assert await observed(phases, services, selected) is services
        assert profile.text_memo.snapshot()["families"]["canonical"]["requests"] == 0
        phases.action_clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
        selected.stack.cursor = 19
        if failure:
            with pytest.raises(LookupError) as raised:
                await observed(phases, services, selected)
            assert raised.value is original_error
        else:
            assert await observed(phases, services, selected) is services
        target_clock = phases.action_clock
        before = profile.text_memo.snapshot()
        phases.action_clock = ContinuationActionClock(minimal_continuation_policy().actions.advisor)
        selected.stack.cursor = 20
        assert await observed(phases, services, selected) is services
        assert profile.text_memo.snapshot() == before
    engine.dispose()
    snapshot = profile.snapshot()
    assert snapshot["review_memo_capture_valid"], snapshot["review_memo_capture_issues"]
    assert not snapshot["cpu_profile_enabled"]
    counts = snapshot["text_memo"]["families"]["canonical"]
    assert counts["requests"] == 2 and counts["misses"] == counts["hits"] == counts["admissions"] == 1
    assert counts["evictions"] == counts["unretained"] == 0
    interval = snapshot["review_memo_interval"]
    assert interval["cursor"] == 19 and interval["clock"] == id(target_clock)
    carriers = tuple(interval["carriers"].values())
    assert {item["kind"] for item in carriers} == {"child", "parent"}
    assert all(item["owner_clock"] == id(target_clock) for item in carriers)
    assert len({item["parent_identity"] for item in carriers}) == 1
    assert interval["callback_ns"] > 0 and snapshot["text_memo"]["observer_ns"] > 0
    assert all(parent["closed_empty"] for parent in snapshot["text_memo"]["parents"])
    assert len(produced) == 4
    assert postfinish == ([] if failure else [1])
    assert interval["preparation_boundary"]["kind"] == ("end_entry_aborted" if failure else "preparation_finish")
    assert "original-sensitive-reference-control" not in json.dumps(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ("worker", "clock", "parent_clock"))
async def test_review_memo_observer_refuses_missing_or_changed_original_witness(monkeypatch, missing):
    phases, selected = _stop_profile_metadata()
    selected.stack.frames = (selected.stack.frames[0], selected.stack.frames[1].model_copy(update={"phase": "review_needed"}))
    selected.stack.cursor = 19
    profile = EndPreparationProfile(enabled=True)
    profile.text_memo = EndTextMemoObservation(include=profile.include_review_memo)
    calls = []
    key = CanonicalTextVerificationKey("control", 1024)

    def verify(_key):
        calls.append(1)

    async def original(actual_phases, actual_services, actual_selected):
        assert actual_phases is phases and actual_selected is selected
        owner_clock = (
            ContinuationActionClock(minimal_continuation_policy().actions.advisor) if missing == "parent_clock" else phases.action_clock
        )
        with v4_preparation_memo_scope(owner_clock) as parent:
            if missing == "worker":
                with ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(parent.canonical_text_verification, key, verify).result(timeout=5)
            elif missing == "clock":
                token = profile._clock.set(None)
                try:
                    parent.canonical_text_verification(key, verify)
                finally:
                    profile._clock.reset(token)
            else:
                parent.canonical_text_verification(key, verify)
        return actual_services

    with profile.text_memo.install(monkeypatch):
        marker = object()
        observed = _driver_review_observer(profile, original, session_id="session", operation_id="operation", cursor=19)
        assert await observed(phases, marker, selected) is marker
    snapshot = profile.snapshot()
    assert calls == [1]  # Diagnostic refusal never suppresses actual work.
    assert snapshot["text_memo"]["families"]["canonical"]["requests"] == 0
    reason = {"worker": "missing_worker_witness", "clock": "wrong_observed_clock", "parent_clock": "wrong_parent_clock"}[missing]
    assert snapshot["review_memo_interval"]["rejections"] == {reason: 1}
    assert not snapshot["review_memo_capture_valid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("spans", "cpu"))
async def test_driver_review_explicit_cursor_applies_to_spans_and_cpu(monkeypatch, mode):
    from elspeth.web.sessions import continuation_driver as driver

    phases, selected = _stop_profile_metadata()
    selected.stack.frames = (selected.stack.frames[0], selected.stack.frames[1].model_copy(update={"phase": "review_needed"}))
    snapshots = []
    marker = object()

    async def original(actual_phases, actual_services, actual_selected):
        assert actual_phases is phases and actual_services is marker and actual_selected is selected
        return marker

    def capture(profile, _path):
        snapshots.append(profile.snapshot())
        return None

    monkeypatch.setattr(driver, "review_prescribed_continuation_end", original)
    monkeypatch.setattr(EndPreparationProfile, "install", lambda *_args: nullcontext())
    monkeypatch.setattr(EndPreparationProfile, "flush", capture)
    monkeypatch.setenv("ELSPETH_TEST_DRIVER_END_PROFILE", mode)
    monkeypatch.setenv("ELSPETH_TEST_DRIVER_REVIEW_CURSOR", "19")
    monkeypatch.setenv("ELSPETH_TEST_DRIVER_END_PROFILE_PATH", ".scratch/review-cursor-control.json")
    with driver_end_preparation_profile(monkeypatch, None, session_id="session", operation_id="operation"):
        selected.stack.cursor = 7
        assert await driver.review_prescribed_continuation_end(phases, marker, selected) is marker
        selected.stack.cursor = 19
        assert await driver.review_prescribed_continuation_end(phases, marker, selected) is marker
    assert driver.review_prescribed_continuation_end is original
    entries = [row for row in snapshots[0]["records"] if row["label"] == "driver.original_review_entry"]
    assert len(entries) == 1 and entries[0]["cursor"] == 19
    assert not snapshots[0]["review_memo_capture_valid"]
    if mode == "spans":
        assert (
            "Review memo capture has no original preparation finish or aborted entry boundary" in snapshots[0]["review_memo_capture_issues"]
        )
