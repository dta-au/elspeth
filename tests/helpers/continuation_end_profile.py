"""Opt-in timing observers for the original END path; no replacement work."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from hashlib import sha256
from pathlib import Path
from types import CodeType, MethodType
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import Engine, event


def _driver_review_observer(profile, original, *, session_id, operation_id, cursor=None):
    from elspeth.web.composer.continuation import EndFrame
    from elspeth.web.composer.continuation_clock import ContinuationActionClock

    @wraps(original)
    async def observed(phases, services, selected):
        root = selected.stack
        clock = phases.action_clock
        if (
            root.logical.session_id != str(session_id)
            or root.logical.operation_id != operation_id
            or (cursor is not None and root.cursor != cursor)
            or type(root.frames[-1]) is not EndFrame
            or root.frames[-1].phase != "review_needed"
            or type(clock) is not ContinuationActionClock
            or clock._seen
            or profile._end_clocks
        ):
            return await original(phases, services, selected)
        with (
            profile.end_entry(clock),
            profile.review_memo_interval(clock, cursor=root.cursor),
            profile.span(
                "driver.original_review_entry",
                cursor=root.cursor,
                original_start_ns=int(clock.started_monotonic * 1_000_000_000),
                entry_lag_ns=time.monotonic_ns() - int(clock.started_monotonic * 1_000_000_000),
                preparation_deadline=clock.preparation_deadline_monotonic,
            ),
        ):
            return await original(phases, services, selected)

    return observed


@contextmanager
def driver_end_preparation_profile(monkeypatch, engine, *, session_id, operation_id):
    """Opt-in existing collector around the original driver's prescribed Review."""
    mode = os.environ.get("ELSPETH_TEST_DRIVER_END_PROFILE")
    if mode is None:
        yield
        return
    assert mode in {"spans", "cpu", "stop_cpu"}, "END driver profile must be spans, cpu or stop_cpu"
    root = Path(__file__).resolve().parents[2]
    output = Path(os.environ.get("ELSPETH_TEST_DRIVER_END_PROFILE_PATH", ".claude/lanes/end-driver-profile/profile.json"))
    if not output.is_absolute():
        output = root / output
    output = output.resolve()
    assert output.is_relative_to(root), "END diagnostic output must remain inside this project"
    from elspeth.web.sessions import continuation_driver as driver

    profile = (
        StopDecisionProfile(enabled=True, cpu_profile=True)
        if mode == "stop_cpu"
        else EndPreparationProfile(enabled=True, cpu_profile=mode == "cpu", cpu_selection="all_workers")
    )
    if mode == "spans":
        profile.text_memo = EndTextMemoObservation(include=profile.include_review_memo)
    try:
        with (
            profile.install(monkeypatch, engine),
            profile.text_memo.install(monkeypatch) if profile.text_memo is not None else nullcontext(),
            monkeypatch.context() as owned,
        ):
            if mode == "stop_cpu":
                cursor = int(os.environ.get("ELSPETH_TEST_DRIVER_STOP_CURSOR", "27"))
                assert cursor >= 0
                owned.setattr(
                    driver,
                    "run_continuation_semantic_advisor",
                    _driver_stop_observer(
                        profile,
                        driver.run_continuation_semantic_advisor,
                        session_id=session_id,
                        operation_id=operation_id,
                        cursor=cursor,
                    ),
                )
            else:
                owned.setattr(
                    driver,
                    "review_prescribed_continuation_end",
                    _driver_review_observer(
                        profile,
                        driver.review_prescribed_continuation_end,
                        session_id=session_id,
                        operation_id=operation_id,
                        cursor=(
                            int(os.environ["ELSPETH_TEST_DRIVER_REVIEW_CURSOR"])
                            if "ELSPETH_TEST_DRIVER_REVIEW_CURSOR" in os.environ
                            else 19
                            if mode == "spans"
                            else None
                        ),
                    ),
                )
            yield
    except BaseException as original:
        profile.case_outcome = {"status": "raised", "error": type(original).__name__}
        raise
    else:
        profile.case_outcome = {"status": "returned"}
    finally:
        try:
            failure = profile.flush(output)
        except Exception as output_error:
            failure = output_error
        if failure is not None:
            print("END driver profile write failed: " + type(failure).__name__)


class EndTextMemoObservation:
    """Bounded test-only cache accounting; no payloads or producer replacements.

    Fingerprints use Python's cached text/key hash and producer identities.
    Collisions can overcount recomputations or undercount distinct text weight;
    these observations never authorize cache hits or product behavior.
    """

    families = ("full", "canonical", "block", "request", "credential", "evidence")

    def __init__(self, *, max_fingerprints: int = 1024, include=None) -> None:
        self.max_fingerprints = max_fingerprints
        self.include = include
        self._lock = threading.Lock()
        self._parents = WeakKeyDictionary()
        self._parent_states = {}
        self._fingerprints = OrderedDict()
        self._active = {}
        self.fingerprint_drops = 0
        self.observer_errors = 0
        self.overlapping_calls = 0
        self.budget_violations = 0
        self.observer_ns = 0
        self.peak_retained_bytes = 0
        self.reverse_requests = {"requests": 0, "hits": 0, "misses": 0, "errors": 0}
        self.counts = {
            family: {
                "requests": 0,
                "hits": 0,
                "misses": 0,
                "errors": 0,
                "admissions": 0,
                "evictions": 0,
                "evicted_bytes": 0,
                "recomputations": 0,
                "unretained": 0,
            }
            for family in self.families
        }

    def _state(self, memo, family, cache_key):
        with memo._lock:
            entries = (
                memo._canonical_proofs,
                memo._canonical_verifications,
                memo._recorder_block_hashes,
                memo._v4_recorder_requests,
                memo._credential_strings,
                memo._evidence_references,
            )
            weights = (
                memo._canonical_proof_bytes,
                memo._canonical_verification_bytes,
                memo._recorder_block_hash_bytes,
                memo._v4_recorder_request_bytes,
                memo._credential_string_bytes,
                memo._evidence_reference_bytes,
            )
            selected = entries[self.families.index(family)]
            hit = cache_key in selected
            value = selected[cache_key] if hit else None
            weight = value.retained_bytes if hit and family in {"full", "request", "credential", "evidence"} else (value if hit else 0)
            return hit, weight, tuple(len(items) for items in entries), weights, memo._shared_text_budget_bytes, not memo._closed

    def _observe(self, memo, family, key, producer, digest_producer, operation):
        if self.include is not None and not self.include(memo, family):
            return operation()
        if memo._v4_preparation_owner is None:
            return operation()
        before_ns = time.monotonic_ns()
        cache_key = (producer, digest_producer, key) if family in {"full", "request"} else (producer, key)
        try:
            before = self._state(memo, family, cache_key)
            dependency_fingerprint = hash(digest_producer) if family == "request" else id(digest_producer)
            fingerprint = (family, id(producer), dependency_fingerprint, hash(key))
            with self._lock:
                ordinal = self._parents.get(memo)
                if ordinal is None:
                    if len(self._parent_states) >= 32:
                        raise ValueError("text observer parent capacity exceeded")
                    ordinal = len(self._parent_states) + 1
                    self._parents[memo] = ordinal
                    self._parent_states[ordinal] = {"budget": before[4], "closed_empty": False}
                fingerprint = (ordinal, *fingerprint)
                self.overlapping_calls += self._active.get(ordinal, 0) > 0
                self._active[ordinal] = self._active.get(ordinal, 0) + 1
                self.counts[family]["requests"] += 1
                self.counts[family]["hits" if before[0] else "misses"] += 1
                self.observer_ns += time.monotonic_ns() - before_ns
        except Exception:
            with self._lock:
                self.observer_errors += 1
            return operation()
        success = False
        try:
            result = operation()
            success = True
            return result
        finally:
            after_ns = time.monotonic_ns()
            try:
                after = self._state(memo, family, cache_key)
                with self._lock:
                    self._active[ordinal] -= 1
                    self.counts[family]["errors"] += not success
                    self.peak_retained_bytes = max(self.peak_retained_bytes, sum(before[3]), sum(after[3]))
                    self.budget_violations += sum(after[3]) > after[4]
                    if success:
                        admitted = not before[0] and after[0]
                        self.counts[family]["admissions"] += admitted
                        self.counts[family]["unretained"] += not after[0]
                        seen = fingerprint in self._fingerprints
                        self.counts[family]["recomputations"] += not before[0] and seen
                        self._fingerprints[fingerprint] = max(self._fingerprints.get(fingerprint, 0), after[1])
                        self._fingerprints.move_to_end(fingerprint)
                        if len(self._fingerprints) > self.max_fingerprints:
                            self._fingerprints.popitem(last=False)
                            self.fingerprint_drops += 1
                        if after[5]:
                            for index, evicted_family in enumerate(self.families):
                                added = admitted and family == evicted_family
                                self.counts[evicted_family]["evictions"] += max(0, before[2][index] + added - after[2][index])
                                self.counts[evicted_family]["evicted_bytes"] += max(
                                    0, before[3][index] + (after[1] if added else 0) - after[3][index]
                                )
            except Exception:
                with self._lock:
                    self.observer_errors += 1
            finally:
                with self._lock:
                    self.observer_ns += time.monotonic_ns() - after_ns

    @contextmanager
    def install(self, monkeypatch):
        from elspeth.web.continuation_retention_memo import RecorderRequestTextDependencies, RetentionStageMemo

        original_full = RetentionStageMemo.canonical_text_proof
        original_canonical = RetentionStageMemo.canonical_text_verification
        original_block = RetentionStageMemo.recorder_block_hash
        original_request = RetentionStageMemo.v4_recorder_request_text
        original_reverse = RetentionStageMemo.v4_restored_recorder_request_text
        original_credential = RetentionStageMemo.credential_string_category
        original_evidence = RetentionStageMemo.evidence_references
        original_close = RetentionStageMemo.close

        def full(memo, key, producer, digest_producer):
            return self._observe(memo, "full", key, producer, digest_producer, lambda: original_full(memo, key, producer, digest_producer))

        def canonical(memo, key, producer):
            return self._observe(memo, "canonical", key, producer, None, lambda: original_canonical(memo, key, producer))

        def block(memo, key, producer):
            return self._observe(memo, "block", key, producer, None, lambda: original_block(memo, key, producer))

        def request(memo, key, dependencies, producer):
            if type(dependencies) is not RecorderRequestTextDependencies or not dependencies.cacheable():
                return original_request(memo, key, dependencies, producer)
            return self._observe(memo, "request", key, producer, dependencies, lambda: original_request(memo, key, dependencies, producer))

        def credential(memo, text, classifier):
            return self._observe(memo, "credential", text, classifier, None, lambda: original_credential(memo, text, classifier))

        def reverse(memo, document, bound, dependencies, producer):
            if memo._v4_preparation_owner is None or (self.include is not None and not self.include(memo, "request")):
                return original_reverse(memo, document, bound, dependencies, producer)
            try:
                result = original_reverse(memo, document, bound, dependencies, producer)
            except BaseException:
                with self._lock:
                    self.reverse_requests["requests"] += 1
                    self.reverse_requests["errors"] += 1
                raise
            before_ns = time.monotonic_ns()
            with self._lock:
                self.reverse_requests["requests"] += 1
                self.reverse_requests["misses" if result is None else "hits"] += 1
                self.observer_ns += time.monotonic_ns() - before_ns
            return result

        def evidence(memo, key, producer):
            return self._observe(memo, "evidence", key, producer, None, lambda: original_evidence(memo, key, producer))

        def close(memo):
            original_close(memo)
            state = self._state(memo, "full", None)
            with self._lock:
                ordinal = self._parents.get(memo)
                if ordinal is not None:
                    self._parent_states[ordinal]["closed_empty"] = (
                        not memo.active
                        and memo.cached_entry_count == 0
                        and not any(state[2])
                        and not any(state[3])
                        and not memo._v4_recorder_request_reverse
                    )

        with monkeypatch.context() as patch:
            patch.setattr(RetentionStageMemo, "canonical_text_proof", full)
            patch.setattr(RetentionStageMemo, "canonical_text_verification", canonical)
            patch.setattr(RetentionStageMemo, "recorder_block_hash", block)
            patch.setattr(RetentionStageMemo, "v4_recorder_request_text", request)
            patch.setattr(RetentionStageMemo, "v4_restored_recorder_request_text", reverse)
            patch.setattr(RetentionStageMemo, "credential_string_category", credential)
            patch.setattr(RetentionStageMemo, "evidence_references", evidence)
            patch.setattr(RetentionStageMemo, "close", close)
            yield self

    def snapshot(self):
        with self._lock:
            return {
                "families": {family: dict(counts) for family, counts in self.counts.items()},
                "reverse_request_lookup": dict(self.reverse_requests),
                "parents": [dict(state) for state in self._parent_states.values()],
                "peak_retained_bytes": self.peak_retained_bytes,
                "observed_key_weight_estimate": None if self.fingerprint_drops else sum(self._fingerprints.values()),
                "fingerprints": len(self._fingerprints),
                "fingerprint_capacity": self.max_fingerprints,
                "fingerprint_drops": self.fingerprint_drops,
                "observer_errors": self.observer_errors,
                "overlapping_calls": self.overlapping_calls,
                "budget_violations": self.budget_violations,
                "observer_ns": self.observer_ns,
                "scope": "retained-key weight estimate uses Python hashes; collisions and dropped fingerprints limit identity claims; overlapping calls invalidate eviction attribution",
            }


class EndPreparationProfile:
    FIRST_CAPTURE_PARENT = "elspeth.web.composer.continuation_checkpoint_end_physical.capture_continuation_runtime_owners"

    def __init__(self, *, enabled: bool, cpu_profile: bool = False, cpu_selection: str = "all_workers") -> None:
        if cpu_selection not in {"all_workers", "checkpoint_source", "first_capture", "selected_scope"}:
            raise ValueError("CPU selection must be all_workers, checkpoint_source, first_capture or selected_scope")
        self.enabled = enabled
        self.cpu_profile = cpu_profile
        self.cpu_selection = cpu_selection
        self.records: list[dict[str, Any]] = []
        self.clocks: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.case_outcome: dict[str, Any] = {"status": "not_completed"}
        self._lock = threading.RLock()
        self._parents: ContextVar[tuple[int, ...]] = ContextVar("end_profile_parents", default=())
        self._clock: ContextVar[int | None] = ContextVar("end_profile_clock", default=None)
        self._workers: dict[int, dict[str, Any]] = {}
        self._submissions: dict[str, dict[str, Any]] = {}
        self._sql: dict[int, dict[str, Any]] = {}
        self._end_clocks: set[int | None] = set()
        self._closed_clocks: dict[int, int] = {}
        self._end_exits: dict[int, dict[str, Any]] = {}
        self._cpu_profiles: list[tuple[dict[str, Any], Any]] = []
        self._first_capture_clocks: set[int] = set()
        self.installed = False
        self.text_memo: EndTextMemoObservation | None = None
        self.text_memo_budget_bytes: int | None = None
        self.review_memo: dict[str, Any] | None = None
        self._review_memo_active = False

    @contextmanager
    def review_memo_interval(self, clock, *, cursor):
        if self.text_memo is None:
            yield
            return
        self.review_memo = {
            "clock": id(clock),
            "cursor": cursor,
            "thread": threading.get_native_id(),
            "start_ns": time.monotonic_ns(),
            "carriers": {},
            "carrier_drops": 0,
            "rejections": {},
            "callback_ns": 0,
        }
        self._review_memo_active = True
        try:
            yield
        finally:
            with self._lock:
                self._review_memo_active = False
                self.review_memo["end_ns"] = time.monotonic_ns()

    def include_review_memo(self, memo, family):
        started = time.monotonic_ns()
        with self._lock:
            interval = self.review_memo
            if not self._review_memo_active or interval is None:
                return False
            if interval["clock"] in self._closed_clocks:
                return False
            thread = threading.get_native_id()
            worker = self._workers.get(thread)
            observed_clock = self._clock.get() if worker is None else worker["clock"]
            parent = memo._text_parent if memo._text_parent is not None else memo
            owner = parent._v4_preparation_owner
            if observed_clock != interval["clock"] and (owner is None or id(owner) != interval["clock"]):
                return False  # Unrelated concurrent work is not this Review's observation.
            reason = None
            if worker is None and thread != interval["thread"]:
                reason = "missing_worker_witness"
            elif observed_clock != interval["clock"]:
                reason = "wrong_observed_clock"
            if reason is not None:
                interval["rejections"][reason] = interval["rejections"].get(reason, 0) + 1
                interval["callback_ns"] += time.monotonic_ns() - started
                return False
            carriers = interval["carriers"]
            identity = id(memo)
            if identity not in carriers and len(carriers) < 128:
                carriers[identity] = {
                    "identity": identity,
                    "parent_identity": id(parent) if owner is not None else None,
                    "owner_clock": None if owner is None else id(owner),
                    "kind": "child" if memo._text_parent is not None else "parent" if owner is not None else "ordinary",
                    "active_at_first_call": memo.active,
                    "families": {},
                }
            if identity in carriers:
                carriers[identity]["active_at_last_call"] = memo.active
                counts = carriers[identity]["families"]
                counts[family] = counts.get(family, 0) + 1
            else:
                interval["carrier_drops"] += 1
            accepted = owner is not None and id(owner) == interval["clock"]
            if owner is not None and id(owner) != interval["clock"]:
                interval["rejections"]["wrong_parent_clock"] = interval["rejections"].get("wrong_parent_clock", 0) + 1
            interval["callback_ns"] += time.monotonic_ns() - started
            return accepted

    def begin(self, label: str, *, kind: str = "call", **facts: Any) -> dict[str, Any]:
        wall = time.monotonic_ns()
        cpu = time.thread_time_ns()
        thread = threading.get_native_id()
        with self._lock:
            worker = self._workers.get(thread)
            row = {
                "id": len(self.records),
                "parent": self._parents.get()[-1] if self._parents.get() else None,
                "clock": self._clock.get() if worker is None else worker["clock"],
                "thread": thread,
                "worker": None if worker is None else worker["id"],
                "kind": kind,
                "label": label,
                "start_ns": wall,
                "thread_cpu_start_ns": cpu,
                **facts,
            }
            self.records.append(row)
            return row

    def end(self, row: dict[str, Any], error: BaseException | None = None) -> None:
        wall = time.monotonic_ns()
        cpu = time.thread_time_ns()
        with self._lock:
            row.update(end_ns=wall, elapsed_ns=wall - row["start_ns"], error=None if error is None else type(error).__name__)
            if threading.get_native_id() == row["thread"]:
                row["thread_cpu_ns"] = cpu - row["thread_cpu_start_ns"]
            else:
                self.errors.append("span resumed on another thread: " + row["label"])

    @contextmanager
    def span(self, label: str, **facts: Any) -> Iterator[dict[str, Any]]:
        row = self.begin(label, **facts)
        token = self._parents.set((*self._parents.get(), row["id"]))
        error = None
        try:
            yield row
        except BaseException as original:
            error = original
            raise
        finally:
            self.end(row, error)
            self._parents.reset(token)

    def sync[**P, T](self, label: str, function: Callable[P, T]) -> Callable[P, T]:
        if not self.enabled:
            return function

        @wraps(function)
        def observed(*args: P.args, **kwargs: P.kwargs) -> T:
            with self.span(label):
                return function(*args, **kwargs)

        return observed

    def asynchronous[**P, T](self, label: str, function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        if not self.enabled:
            return function

        @wraps(function)
        async def observed(*args: P.args, **kwargs: P.kwargs) -> T:
            with self.span(label):
                return await function(*args, **kwargs)

        return observed

    def _before_sql(self, _connection, _cursor, statement, _parameters, context, executemany) -> None:
        with self._lock:
            if id(context) in self._sql:
                self.errors.append("SQL context entered twice without completion")
            self._sql[id(context)] = self.begin(
                "cursor.execute",
                kind="sql",
                template_sha256=sha256(statement.encode("utf-8")).hexdigest(),
                statement_bytes=len(statement.encode("utf-8")),
                executemany=executemany,
            )

    def _after_sql(self, _connection, _cursor, _statement, _parameters, context, _executemany) -> None:
        with self._lock:
            row = self._sql.pop(id(context), None)
        if row is None:
            self.errors.append("SQL completion had no original entry")
        else:
            self.end(row)

    def _sql_error(self, context) -> None:
        with self._lock:
            row = self._sql.pop(id(context.execution_context), None)
        if row is None:
            self.errors.append("SQL error had no original cursor entry")
        else:
            self.end(row, context.original_exception)

    @contextmanager
    def sql(self, engine: Engine) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        callbacks = (
            ("before_cursor_execute", self._before_sql),
            ("after_cursor_execute", self._after_sql),
            ("handle_error", self._sql_error),
        )
        for name, callback in callbacks:
            event.listen(engine, name, callback)
        try:
            yield
        finally:
            for name, callback in callbacks:
                event.remove(engine, name, callback)

    @contextmanager
    def workers(self, monkeypatch) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        from elspeth.web.required_executor import RequiredInvocationWitness

        original_submit = ThreadPoolExecutor.submit
        original_invoke = RequiredInvocationWitness.invoke

        def submit(executor, function, /, *args, **kwargs):
            if not (type(function) is MethodType and isinstance(function.__self__, RequiredInvocationWitness)):
                return original_submit(executor, function, *args, **kwargs)
            witness = function.__self__
            row = self.begin("required.submit", kind="submission", witness=witness.identity)
            row["parents"] = self._parents.get()
            with self._lock:
                if witness.identity in self._submissions:
                    self.errors.append("duplicate physical witness submission")
                self._submissions[witness.identity] = row
            try:
                future = original_submit(executor, function, *args, **kwargs)
            except BaseException as original:
                row["submission_failed"] = True
                self.end(row, original)
                raise
            row["future_identity"] = id(future)
            self.end(row)
            return future

        def invoke(witness, callable_):
            with self._lock:
                submission = self._submissions.get(witness.identity)
            if submission is None:
                self.errors.append("physical worker had no observed submission")
                return original_invoke(witness, callable_)
            parent = self._parents.set(tuple(submission["parents"]))
            clock = self._clock.set(submission["clock"])
            try:
                with self.span("required.worker_including_gate_wait", kind="worker", submission=submission["id"]) as row:
                    row["queue_to_worker_ns"] = row["start_ns"] - submission["start_ns"]
                    with self._lock:
                        submission["worker_id"] = row["id"]
                        self._workers[threading.get_native_id()] = row
                    try:
                        # Preserve this exact witness, callable and gated body.
                        if (
                            self.cpu_profile
                            and self.cpu_selection == "all_workers"
                            and submission["clock"] in self._end_clocks
                            and submission["clock"] not in self._closed_clocks
                            and submission["clock"] not in self._end_exits
                        ):
                            return self._profile_worker(row, lambda: original_invoke(witness, callable_))
                        return original_invoke(witness, callable_)
                    finally:
                        with self._lock:
                            self._workers.pop(threading.get_native_id(), None)
            finally:
                self._clock.reset(clock)
                self._parents.reset(parent)

        with monkeypatch.context() as owned:
            owned.setattr(ThreadPoolExecutor, "submit", submit)
            owned.setattr(RequiredInvocationWitness, "invoke", invoke)
            yield

    def _profile_worker[T](self, row: dict[str, Any], original: Callable[[], T]) -> T:
        with self._worker_cpu_capture(row):
            return original()

    @contextmanager
    def _worker_cpu_capture(self, row: dict[str, Any]) -> Iterator[None]:
        import cProfile

        previous_hook = sys.getprofile()
        row["cprofile_legacy_hook_before"] = None if previous_hook is None else id(previous_hook)
        if previous_hook is not None:
            row["cprofile_refused"] = "existing thread profile hook"
            self.errors.append("worker CPU profile refused an existing thread hook")
            yield
            return
        tool = sys.monitoring.PROFILER_ID
        if sys.monitoring.get_tool(tool) is not None:
            row["cprofile_refused"] = "existing monitoring profiler slot"
            self.errors.append("worker CPU profile refused an existing monitoring profiler slot")
            yield
            return
        owner = threading.get_ident()

        class WorkerProfile(cProfile.Profile):
            # CPython 3.12/3.13 use interpreter-wide cProfile callbacks.
            # Forward only this original worker's events to its thread timer.
            def _pystart_callback(self, *args):
                if threading.get_ident() == owner:
                    return super()._pystart_callback(*args)
                return None

            def _pyreturn_callback(self, *args):
                if threading.get_ident() == owner:
                    return super()._pyreturn_callback(*args)
                return None

            def _ccall_callback(self, *args):
                if threading.get_ident() == owner:
                    return super()._ccall_callback(*args)
                return None

            def _creturn_callback(self, *args):
                if threading.get_ident() == owner:
                    return super()._creturn_callback(*args)
                return None

        profile = WorkerProfile(timer=time.thread_time_ns, timeunit=1e-9, subcalls=True, builtins=True)
        cpu_start = time.thread_time_ns()
        try:
            profile.enable()
        except Exception as setup_error:
            row["cprofile_refused"] = "setup failed: " + type(setup_error).__name__
            self.errors.append("worker CPU profile setup failed")
            yield
            return
        hook_after_enable = sys.getprofile()
        row["cprofile_legacy_hook_after_enable"] = None if hook_after_enable is None else id(hook_after_enable)
        row["cprofile_monitoring_after_enable"] = sys.monitoring.get_tool(tool)
        row["cprofile_monitoring_events_after_enable"] = sys.monitoring.get_events(tool)
        try:
            yield
        finally:
            # disable owns the monitoring slot; sys.getprofile is the separate
            # legacy hook and remains unchanged by cProfile on this runtime.
            profile.disable()
            hook_after_disable = sys.getprofile()
            row["cprofile_legacy_hook_after_disable"] = None if hook_after_disable is None else id(hook_after_disable)
            row["cprofile_monitoring_after_disable"] = sys.monitoring.get_tool(tool)
            row["cprofile_monitoring_events_after_disable"] = sys.monitoring.get_events(tool)
            if row["cprofile_monitoring_after_disable"] is not None or row["cprofile_monitoring_events_after_disable"] != 0:
                self.errors.append("worker CPU profile retained its monitoring profiler slot")
            row["cprofile_enclosing_thread_cpu_ns"] = time.thread_time_ns() - cpu_start
            row["cprofile_timer"] = "time.thread_time_ns"
            row["cprofile_timeunit"] = 1e-9
            with self._lock:
                self._cpu_profiles.append((row, profile))

    def _checkpoint_source_observer(self, original):
        @wraps(original)
        def selected(service, *, prepared):
            with self._lock:
                row = self._workers.get(threading.get_native_id())
            if (
                self.cpu_profile
                and self.cpu_selection == "checkpoint_source"
                and row is not None
                and row["clock"] in self._end_clocks
                and row["clock"] not in self._closed_clocks
                and row["clock"] not in self._end_exits
            ):
                row["cprofile_selected_method"] = {
                    "module": original.__module__,
                    "qualname": original.__qualname__,
                    "file": original.__code__.co_filename,
                    "line": original.__code__.co_firstlineno,
                }
                row["cprofile_scope"] = "original_selected_method_body"
                with self.span("cprofile.selected_method_body") as selected_row:
                    row["cprofile_method_span"] = selected_row["id"]
                    return self._profile_worker(row, lambda: original(service, prepared=prepared))
            return original(service, prepared=prepared)

        return selected

    def _first_capture_scope_observer(self, original_scope, capture_code: CodeType, module: str):
        @contextmanager
        @wraps(original_scope)
        def selected(*args, **kwargs):
            with self._lock:
                row = self._workers.get(threading.get_native_id())
                submission = None if row is None else self.records[row["submission"]]
                eligible = (
                    self.cpu_profile
                    and self.cpu_selection == "first_capture"
                    and row is not None
                    and row["clock"] in self._end_clocks
                    and row["clock"] not in self._closed_clocks
                    and row["clock"] not in self._end_exits
                    and row["clock"] not in self._first_capture_clocks
                    and submission is not None
                    and any(self.records[parent]["label"] == self.FIRST_CAPTURE_PARENT for parent in submission["parents"])
                )
                if eligible:
                    self._first_capture_clocks.add(row["clock"])
            if eligible:
                row["cprofile_selected_method"] = {
                    "module": module,
                    "qualname": capture_code.co_qualname,
                    "file": capture_code.co_filename,
                    "line": capture_code.co_firstlineno,
                }
                row["cprofile_scope"] = "original_first_capture_scope"
                with self.span("cprofile.selected_capture_scope") as selected_row:
                    row["cprofile_method_span"] = selected_row["id"]
                    with self._worker_cpu_capture(row), original_scope(*args, **kwargs) as memo:
                        yield memo
            else:
                with original_scope(*args, **kwargs) as memo:
                    yield memo

        return selected

    @contextmanager
    def end_entry(self, clock) -> Iterator[None]:
        """Observe the actual END entry/exit without changing its action clock."""
        identity = None if clock is None else id(clock)
        if identity is not None:
            self._end_clocks.add(identity)
        token = self._clock.set(identity)
        try:
            with self.span("END.execute_continuation_end_review", clock=identity) as row:
                yield
        finally:
            if identity is not None:
                self._end_exits[identity] = {
                    "end_ns": row["end_ns"],
                    "entry_id": row["id"],
                    "kind": "end_entry_aborted" if row["error"] is not None else "end_entry_returned_without_finish",
                    "error": row["error"],
                }
            self._clock.reset(token)

    @contextmanager
    def install(self, monkeypatch, engine: Engine) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        from elspeth.web.composer import continuation_checkpoint_end_detached as detached
        from elspeth.web.composer import continuation_checkpoint_end_first_return as first_return
        from elspeth.web.composer import continuation_checkpoint_end_locals as gate
        from elspeth.web.composer import continuation_checkpoint_end_physical as physical
        from elspeth.web.composer import continuation_checkpoint_end_producer as producer
        from elspeth.web.composer import continuation_checkpoint_phase as phase
        from elspeth.web.composer import continuation_runtime as runtime
        from elspeth.web.composer import prepared_provider_input as provider_input
        from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
        from elspeth.web.composer.composition_completion import CompositionCompletion
        from elspeth.web.composer.continuation_clock import ContinuationActionClock
        from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
        from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
        from elspeth.web.sessions import continuation_checkpoint_paid as paid
        from elspeth.web.sessions import continuation_end as end
        from elspeth.web.sessions.service import SessionServiceImpl
        from tests.unit.web.sessions import test_continuation_end_shared_execution as shared

        original_clock = ContinuationActionClock.__init__
        original_bind = ContinuationLivePhases.bind_action_clock
        original_provider_input = provider_input.prepare_provider_input
        original_finish = ContinuationActionClock.finish_preparation
        original_end = shared.execute_continuation_end_review
        original_capture_scope = runtime.borrow_v4_preparation_memo_scope
        capture_codes = [
            value
            for value in runtime.capture_continuation_runtime_owners.__code__.co_consts
            if type(value) is CodeType and value.co_name == "capture"
        ]

        async def end_entry(phases, selected):
            with self.end_entry(phases.action_clock):
                return await original_end(phases, selected)

        def finish(clock):
            try:
                with self.span("ContinuationActionClock.finish_preparation", clock=id(clock)) as row:
                    return original_finish(clock)
            finally:
                self._closed_clocks[id(clock)] = row["end_ns"]

        def clock_init(clock, policy):
            with self.span("clock.create") as row:
                original_clock(clock, policy)
                row["clock"] = id(clock)
                row["original_start_ns"] = int(clock.started_monotonic * 1_000_000_000)
                self.clocks.append(
                    {
                        "identity": id(clock),
                        "start_ns": int(clock.started_monotonic * 1_000_000_000),
                        "preparation_deadline": clock.preparation_deadline_monotonic,
                        "total_deadline_loop_time": clock.deadline,
                        "policy": policy.model_dump(mode="json"),
                    }
                )

        def bind(phases, clock):
            original_bind(phases, clock)
            self._clock.set(id(clock))
            with self.span("clock.bound", phase_identity=id(phases), clock=id(clock)) as row:
                deadline = phases.work.coordinator.sql_deadline_owner
                row["sql_hard_expires_at"] = None if deadline is None else deadline.hard_expires_at
                row["turn_deadline"] = None if phases.runtime is None else phases.runtime.bindings.deadline

        synchronous = (
            (gate, "capture_checkpoint_end_gate_locals", gate.capture_checkpoint_end_gate_locals),
            (physical, "bind_checkpoint_end_physical_request", physical.bind_checkpoint_end_physical_request),
            (physical, "require_original_checkpoint_end_physical_source", physical.require_original_checkpoint_end_physical_source),
            (producer, "build_checkpoint_end_input", producer.build_checkpoint_end_input),
            (runtime, "_capture_owners", runtime._capture_owners),
            (provider_input, "prepare_provider_input", provider_input.prepare_provider_input),
            (paid, "prepare_v4_checkpoint_source_on_connection", paid.prepare_v4_checkpoint_source_on_connection),
            (ComposerAsyncOperationAuthority, "read_continuation_runtime", ComposerAsyncOperationAuthority.read_continuation_runtime),
            (AdvisorCheckpointOwner, "_build_checkpoint_arguments", AdvisorCheckpointOwner._build_checkpoint_arguments),
        )
        asynchronous = (
            (end, "_primary", end._primary),
            (end, "_retain", end._retain),
            (phase, "prepare_checkpoint_phase_owner", phase.prepare_checkpoint_phase_owner),
            (ContinuationLivePhases, "current", ContinuationLivePhases.current),
            (CompositionCompletion, "_try_terminate_no_tools", CompositionCompletion._try_terminate_no_tools),
            (AdvisorCheckpointOwner, "_run_advisor_checkpoint", AdvisorCheckpointOwner._run_advisor_checkpoint),
            (AdvisorCheckpointOwner, "_call_advisor_with_audit_body", AdvisorCheckpointOwner._call_advisor_with_audit_body),
            (physical, "capture_continuation_runtime_owners", physical.capture_continuation_runtime_owners),
            (detached, "capture_continuation_runtime_owners", detached.capture_continuation_runtime_owners),
            (detached, "restore_prepared_end_runtime", detached.restore_prepared_end_runtime),
            (first_return, "prepare_checkpoint_end_physical_source", first_return.prepare_checkpoint_end_physical_source),
            (runtime, "_restore_runtime", runtime._restore_runtime),
        )
        self.installed = True
        with monkeypatch.context() as owned, self.sql(engine), self.workers(owned):
            owned.setattr(ContinuationActionClock, "__init__", clock_init)
            owned.setattr(ContinuationLivePhases, "bind_action_clock", bind)
            owned.setattr(ContinuationActionClock, "finish_preparation", finish)
            owned.setattr(shared, "execute_continuation_end_review", end_entry)
            if self.cpu_selection == "first_capture":
                if len(capture_codes) != 1:
                    self.errors.append("CPU capture could not identify the original registered capture code")
                else:
                    owned.setattr(
                        runtime,
                        "borrow_v4_preparation_memo_scope",
                        self._first_capture_scope_observer(
                            original_capture_scope, capture_codes[0], runtime.capture_continuation_runtime_owners.__module__
                        ),
                    )
            for target, name, original in synchronous:
                owned.setattr(target, name, self.sync(target.__name__ + "." + name, original))
            for target, name, original in asynchronous:
                owned.setattr(target, name, self.asynchronous(target.__name__ + "." + name, original))
            # Select the exact original method body in its real witness worker;
            # the caller transports it through a lambda and SQL deadline scope.
            original_source = SessionServiceImpl.prepare_v4_checkpoint_source_sync
            owned.setattr(
                SessionServiceImpl,
                "prepare_v4_checkpoint_source_sync",
                self.sync(
                    SessionServiceImpl.__name__ + ".prepare_v4_checkpoint_source_sync",
                    self._checkpoint_source_observer(original_source),
                ),
            )
            # This imported alias is the actual transport preparation lookup.
            from elspeth.web.composer import advisor_checkpoint

            owned.setattr(
                advisor_checkpoint,
                "prepare_provider_input",
                self.sync("advisor.prepare_provider_input", original_provider_input),
            )
            try:
                yield
            finally:
                self.installed = False

    def _cpu_boundary(self, clock: int | None) -> dict[str, Any] | None:
        if clock in self._closed_clocks:
            return {"end_ns": self._closed_clocks[clock], "kind": "preparation_finish"}
        return self._end_exits.get(clock)

    def _cpu_snapshot(self) -> list[dict[str, Any]]:
        import pstats

        clocks = {clock["identity"]: clock for clock in self.clocks}
        output = []
        for row, profile in self._cpu_profiles:
            clock = clocks.get(row["clock"])
            boundary = self._cpu_boundary(row["clock"])
            stop = None if boundary is None else boundary["end_ns"]
            within_window = (
                clock is not None and stop is not None and clock["start_ns"] <= row["start_ns"] <= row.get("end_ns", stop + 1) <= stop
            )
            entries = []
            for (filename, line, name), (primitive, calls, own, cumulative, callers) in pstats.Stats(profile).stats.items():
                entries.append(
                    {
                        "file": filename,
                        "line": line,
                        "function": name,
                        "primitive_calls": primitive,
                        "total_calls": calls,
                        "self_thread_cpu_seconds": own,
                        "cumulative_thread_cpu_seconds": cumulative,
                        "callers": [
                            {"file": source[0], "line": source[1], "function": source[2], "stats": list(values)}
                            for source, values in callers.items()
                        ],
                    }
                )
            output.append(
                {
                    "worker_id": row["id"],
                    "submission_id": row["submission"],
                    "witness": self.records[row["submission"]]["witness"],
                    "clock": row["clock"],
                    "thread": row["thread"],
                    "clock_start_ns": None if clock is None else clock["start_ns"],
                    "preparation_finish_ns": self._closed_clocks.get(row["clock"]),
                    "capture_boundary": boundary,
                    "within_preparation_window": within_window,
                    "parent_context": [self.records[key]["label"] for key in self.records[row["submission"]]["parents"]],
                    "timer": "time.thread_time_ns",
                    "timeunit": 1e-9,
                    "selected_method": row.get("cprofile_selected_method"),
                    "selected_method_span": row.get("cprofile_method_span"),
                    "scope": (
                        "original first physical capture memo scope in original worker; queue and gate excluded; "
                        "initial capture Python frame entry excluded; both authority reads and _capture_owners included; "
                        "cProfile overhead included; complete library/builtin/caller stats"
                        if row.get("cprofile_scope") == "original_first_capture_scope"
                        else "original current-evidence memo scope in original worker; queue and gate excluded; "
                        "initial method Python frame entry excluded; both fresh authority reads and evidence restoration included; "
                        "cProfile overhead included; complete library/builtin/caller stats"
                        if row.get("cprofile_scope") == "original_current_evidence_scope"
                        else "original selected method body in original worker; gate excluded; "
                        "cProfile overhead included; complete library/builtin/caller stats"
                        if row.get("cprofile_scope") == "original_selected_method_body"
                        else "original worker including gate; cProfile overhead included; complete library/builtin/caller stats"
                    ),
                    "entries": entries,
                }
            )
        return output

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cpu_profiles = self._cpu_snapshot()
            clock_starts = {clock["identity"]: clock["start_ns"] for clock in self.clocks}
            boundaries = {clock: self._cpu_boundary(clock) for clock in self._end_clocks}
            expected_cpu = [
                row["id"]
                for row in self.records
                if self.cpu_profile
                and row["kind"] == "worker"
                and (self.cpu_selection == "all_workers" or "cprofile_selected_method" in row)
                and row["clock"] in self._end_clocks
                and row["clock"] in clock_starts
                and clock_starts[row["clock"]] <= row["start_ns"]
                and (boundaries[row["clock"]] is None or row["start_ns"] <= boundaries[row["clock"]]["end_ns"])
            ]
            missing_cpu = sorted(set(expected_cpu) - {profile["worker_id"] for profile in cpu_profiles})
            cpu_issues = []
            if self.cpu_profile:
                if not self.enabled:
                    cpu_issues.append("CPU capture requires enabled timing observations")
                if not self._end_clocks or None in self._end_clocks:
                    cpu_issues.append("CPU capture has no original END clock target")
                for clock in self._end_clocks:
                    if clock not in clock_starts:
                        cpu_issues.append(f"CPU target {clock} has no observed original clock start")
                    if boundaries[clock] is None:
                        cpu_issues.append(f"CPU target {clock} has no preparation-finish or END-entry-exit boundary")
                    elif boundaries[clock]["kind"] == "end_entry_returned_without_finish":
                        cpu_issues.append(f"CPU target {clock} returned without preparation finish")
                if not expected_cpu:
                    cpu_issues.append("CPU capture has no expected original END workers")
                    if self.cpu_selection == "checkpoint_source":
                        cpu_issues.append("CPU capture did not reach the original selected checkpoint-source method")
                    elif self.cpu_selection == "first_capture":
                        cpu_issues.append("CPU capture did not reach the original first physical capture scope")
                if missing_cpu:
                    cpu_issues.append("CPU capture is missing expected original END workers")
                if any(not profile["within_preparation_window"] for profile in cpu_profiles):
                    cpu_issues.append("CPU capture contains workers without a complete original preparation window")
                if self.errors:
                    cpu_issues.append("CPU capture has observer errors")
            cpu_valid = self.cpu_profile and not cpu_issues
            cpu_status = "disabled" if not self.cpu_profile else "invalid"
            if cpu_valid:
                cpu_status = (
                    "captured_aborted_entry"
                    if any(boundary["kind"] == "end_entry_aborted" for boundary in boundaries.values())
                    else "captured_preparation"
                )
            incomplete = [row["id"] for row in self.records if "end_ns" not in row]
            unpaired = [
                row["id"]
                for row in self.records
                if row["kind"] == "submission" and not row.get("submission_failed") and "worker_id" not in row
            ]
            memo_snapshot = None if self.text_memo is None else self.text_memo.snapshot()
            interval = self.review_memo
            memo_issues = []
            if interval is not None and memo_snapshot is not None:
                boundary = self._cpu_boundary(interval["clock"])
                if boundary is None or boundary["kind"] == "end_entry_returned_without_finish":
                    memo_issues.append("Review memo capture has no original preparation finish or aborted entry boundary")
                interval["preparation_boundary"] = boundary
                if "end_ns" not in interval or not interval["carriers"]:
                    memo_issues.append("Review memo interval has no complete observed carrier calls")
                if interval["rejections"] or interval["carrier_drops"]:
                    memo_issues.append("Review memo interval has rejected witnesses or dropped carriers")
                if any(not parent["closed_empty"] for parent in memo_snapshot["parents"]):
                    memo_issues.append("Review memo parent did not close empty")
                if any(memo_snapshot[key] for key in ("observer_errors", "overlapping_calls", "budget_violations", "fingerprint_drops")):
                    memo_issues.append("Review memo accounting is incomplete or inconsistent")
                if self.errors or any(self.records[index]["clock"] == interval["clock"] for index in incomplete + unpaired):
                    memo_issues.append("Review memo interval has incomplete transport or observer errors")
            return {
                "enabled": self.enabled,
                "cpu_profile_enabled": self.cpu_profile,
                "cpu_selection": self.cpu_selection,
                "cpu_capture_valid": cpu_valid,
                "cpu_capture_status": cpu_status,
                "cpu_capture_issues": cpu_issues,
                "cpu_target_clocks": sorted(clock for clock in self._end_clocks if clock is not None),
                "cpu_target_boundaries": [{"clock": clock, "boundary": boundary} for clock, boundary in boundaries.items()],
                "cpu_profiles": cpu_profiles,
                "cpu_expected_workers": expected_cpu,
                "cpu_missing_workers": missing_cpu,
                "case_outcome": dict(self.case_outcome),
                "clocks": list(self.clocks),
                "records": [dict(row) for row in self.records],
                "errors": list(self.errors),
                "incomplete_spans": incomplete,
                "unpaired_submissions": unpaired,
                "pending_sql_contexts": len(self._sql),
                "cpu_scope": "native thread CPU; async intervals include other event-loop activity; worker body includes gate wait",
                "wall_scope": "inclusive intervals; do not sum nested or overlapping spans",
                "text_memo": memo_snapshot,
                "text_memo_budget_bytes": self.text_memo_budget_bytes,
                "review_memo_interval": self.review_memo,
                "review_memo_capture_valid": interval is not None and memo_snapshot is not None and not memo_issues,
                "review_memo_capture_issues": memo_issues,
            }

    def flush(self, path: Path) -> OSError | None:
        """Return an I/O failure so the diagnostic never replaces an original error."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.snapshot(), indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            return error
        return None


def _driver_stop_observer(profile, original, *, session_id, operation_id, cursor):
    from elspeth.web.composer.continuation import AdvisorFrame, EndFrame
    from elspeth.web.composer.continuation_clock import ContinuationActionClock

    @wraps(original)
    async def observed(phases, selected):
        root = selected.stack
        clock = phases.action_clock
        if (
            root.logical.session_id != str(session_id)
            or root.logical.operation_id != operation_id
            or root.cursor != cursor
            or len(root.frames) != 3
            or type(root.frames[-1]) is not AdvisorFrame
            or root.frames[-1].phase != "response_retained"
            or type(root.frames[-2]) is not EndFrame
            or root.frames[-2].phase != "publication_ready"
            or type(clock) is not ContinuationActionClock
            or clock._seen
            or profile._end_clocks
        ):
            return await original(phases, selected)
        # Observe the clock already created/bound by the real driver. No new
        # timer, SQL deadline, stage, memo or provider ownership is installed.
        profile.clocks.append({"identity": id(clock), "start_ns": int(clock.started_monotonic * 1_000_000_000)})
        with profile.end_entry(clock), profile.span("driver.original_stop_decision", cursor=root.cursor):
            return await original(phases, selected)

    return observed


class StopDecisionProfile(EndPreparationProfile):
    """Original final decision SQL action, deliberately not a preparation capture."""

    @contextmanager
    def end_entry(self, clock):
        identity = id(clock)
        self._end_clocks.add(identity)
        token = self._clock.set(identity)
        try:
            with self.span("SQL_ACTION.semantic_stop_decision", clock=identity) as row:
                yield
        finally:
            self._end_exits[identity] = {
                "end_ns": row["end_ns"],
                "entry_id": row["id"],
                "kind": "sql_action_aborted" if row["error"] is not None else "sql_action_returned",
                "error": row["error"],
            }
            self._clock.reset(token)

    def _cpu_boundary(self, clock):
        return self._end_exits.get(clock)

    @contextmanager
    def install(self, monkeypatch, engine):
        from elspeth.web.composer import continuation_semantic_advisor_phase as phase

        with monkeypatch.context() as owned, self.sql(engine), self.workers(owned):
            owned.setattr(
                phase,
                "install_current_semantic_decision_on_connection",
                self.sync("stop.install_decision", phase.install_current_semantic_decision_on_connection),
            )
            owned.setattr(
                phase,
                "consume_semantic_advisor_end_stop_on_connection",
                self.sync("stop.consume_end", phase.consume_semantic_advisor_end_stop_on_connection),
            )
            self.installed = True
            try:
                yield
            finally:
                self.installed = False

    def snapshot(self):
        result = super().snapshot()
        issues = result["cpu_capture_issues"]
        target_submissions = [row for row in self.records if row["kind"] == "submission" and row["clock"] in self._end_clocks]
        if any(not row.get("submission_failed") and "worker_id" not in row for row in target_submissions):
            issues.append("SQL action has an unpaired original worker submission")
        if any(row["clock"] in self._end_clocks and "end_ns" not in row for row in self.records):
            issues.append("SQL action has incomplete original spans")
        if result["pending_sql_contexts"]:
            issues.append("SQL action has pending SQL observations")
        result["cpu_capture_valid"] = self.cpu_profile and not issues
        result["cpu_capture_status"] = (
            "captured_sql_action_aborted"
            if result["cpu_capture_valid"] and any(boundary["kind"] == "sql_action_aborted" for boundary in self._end_exits.values())
            else "captured_sql_action"
            if result["cpu_capture_valid"]
            else "invalid"
        )
        result["diagnostic_scope"] = "original selected SQL action entry through return/error; not preparation or SDK acceptance"
        result["cpu_scope"] = (
            "each original native worker thread CPU including gate; install/consume spans are nested, not additive; "
            "event-loop CPU is NOT profiled; remaining action wall is not attributed to a worker; cProfile overhead included"
        )
        for captured in result["cpu_profiles"]:
            captured["within_sql_action_window"] = captured.pop("within_preparation_window")
            captured.pop("preparation_finish_ns")
        return result
