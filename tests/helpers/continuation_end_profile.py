"""Opt-in timing observers for the original END path; no replacement work."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from hashlib import sha256
from pathlib import Path
from types import MethodType
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import Engine, event


class EndTextMemoObservation:
    """Bounded test-only cache accounting; no payloads or producer replacements.

    Fingerprints use Python's cached text/key hash and producer identities.
    Collisions can overcount recomputations or undercount distinct text weight;
    these observations never authorize cache hits or product behavior.
    """

    families = ("full", "canonical", "block")

    def __init__(self, *, max_fingerprints: int = 1024) -> None:
        self.max_fingerprints = max_fingerprints
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
            entries = (memo._canonical_proofs, memo._canonical_verifications, memo._recorder_block_hashes)
            weights = (memo._canonical_proof_bytes, memo._canonical_verification_bytes, memo._recorder_block_hash_bytes)
            selected = entries[self.families.index(family)]
            hit = cache_key in selected
            value = selected[cache_key] if hit else None
            weight = value.retained_bytes if hit and family == "full" else (value if hit else 0)
            return hit, weight, tuple(len(items) for items in entries), weights, memo._shared_text_budget_bytes, not memo._closed

    def _observe(self, memo, family, key, producer, digest_producer, operation):
        if memo._v4_preparation_owner is None:
            return operation()
        before_ns = time.monotonic_ns()
        cache_key = (producer, digest_producer, key) if family == "full" else (producer, key)
        try:
            before = self._state(memo, family, cache_key)
            fingerprint = (family, id(producer), id(digest_producer), hash(key))
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
        from elspeth.web.continuation_retention_memo import RetentionStageMemo

        original_full = RetentionStageMemo.canonical_text_proof
        original_canonical = RetentionStageMemo.canonical_text_verification
        original_block = RetentionStageMemo.recorder_block_hash
        original_close = RetentionStageMemo.close

        def full(memo, key, producer, digest_producer):
            return self._observe(memo, "full", key, producer, digest_producer, lambda: original_full(memo, key, producer, digest_producer))

        def canonical(memo, key, producer):
            return self._observe(memo, "canonical", key, producer, None, lambda: original_canonical(memo, key, producer))

        def block(memo, key, producer):
            return self._observe(memo, "block", key, producer, None, lambda: original_block(memo, key, producer))

        def close(memo):
            original_close(memo)
            with self._lock:
                ordinal = self._parents.get(memo)
                if ordinal is not None:
                    self._parent_states[ordinal]["closed_empty"] = not memo.active and memo.cached_entry_count == 0

        with monkeypatch.context() as patch:
            patch.setattr(RetentionStageMemo, "canonical_text_proof", full)
            patch.setattr(RetentionStageMemo, "canonical_text_verification", canonical)
            patch.setattr(RetentionStageMemo, "recorder_block_hash", block)
            patch.setattr(RetentionStageMemo, "close", close)
            yield self

    def snapshot(self):
        with self._lock:
            return {
                "families": {family: dict(counts) for family, counts in self.counts.items()},
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
    def __init__(self, *, enabled: bool, cpu_profile: bool = False, cpu_selection: str = "all_workers") -> None:
        if cpu_selection not in {"all_workers", "checkpoint_source"}:
            raise ValueError("CPU selection must be all_workers or checkpoint_source")
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
        self.installed = False
        self.text_memo: EndTextMemoObservation | None = None
        self.text_memo_budget_bytes: int | None = None

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
        import cProfile

        previous_hook = sys.getprofile()
        row["cprofile_legacy_hook_before"] = None if previous_hook is None else id(previous_hook)
        if previous_hook is not None:
            row["cprofile_refused"] = "existing thread profile hook"
            self.errors.append("worker CPU profile refused an existing thread hook")
            return original()
        tool = sys.monitoring.PROFILER_ID
        if sys.monitoring.get_tool(tool) is not None:
            row["cprofile_refused"] = "existing monitoring profiler slot"
            self.errors.append("worker CPU profile refused an existing monitoring profiler slot")
            return original()
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
            return original()
        hook_after_enable = sys.getprofile()
        row["cprofile_legacy_hook_after_enable"] = None if hook_after_enable is None else id(hook_after_enable)
        row["cprofile_monitoring_after_enable"] = sys.monitoring.get_tool(tool)
        row["cprofile_monitoring_events_after_enable"] = sys.monitoring.get_events(tool)
        try:
            return original()
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
                        "original selected method body in original worker; gate excluded; "
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
                "text_memo": None if self.text_memo is None else self.text_memo.snapshot(),
                "text_memo_budget_bytes": self.text_memo_budget_bytes,
            }

    def flush(self, path: Path) -> OSError | None:
        """Return an I/O failure so the diagnostic never replaces an original error."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.snapshot(), indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            return error
        return None
