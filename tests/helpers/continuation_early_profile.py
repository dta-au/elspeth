"""Opt-in EARLY timing at original clocks, source boundaries and SQL workers.

Mode zero observes only inclusive clock-to-preparation-finish time. Mode one
adds selected inclusive spans, native thread CPU, SQL and the already calibrated
original-witness observer, plus six-family memo statistics. Mode two records
the same spans without the memo observer. A separate CPU opt-in, available
in either span mode, captures the first current-evidence memo scope, the first
message-inputs call, or every original required worker. No mode
replaces submitted callables, adjusts a deadline, consumes a clock stage,
caches a result or records private payloads.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager, nullcontext
from functools import wraps
from pathlib import Path
from typing import Any

from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.continuation_retention_memo import current_v4_preparation_memo
from tests.helpers.continuation_end_profile import EndPreparationProfile, EndTextMemoObservation

PROFILE_FLAG = "ELSPETH_TEST_EARLY_PREPARATION_PROFILE"
PROFILE_DIRECTORY = "ELSPETH_TEST_EARLY_PREPARATION_PROFILE_DIRECTORY"
CPU_PROFILE_FLAG = "ELSPETH_TEST_EARLY_PREPARATION_CPU"
SEMANTIC_PROFILE_FLAG = "ELSPETH_TEST_SEMANTIC_PREPARATION_PROFILE"
SEMANTIC_PROFILE_DIRECTORY = "ELSPETH_TEST_SEMANTIC_PREPARATION_PROFILE_DIRECTORY"


def _cpu_sample() -> dict[str, int]:
    before = time.monotonic_ns()
    thread, process = time.thread_time_ns(), time.process_time_ns()
    return {"wall_before_ns": before, "wall_after_ns": time.monotonic_ns(), "thread_ns": thread, "process_ns": process}


class EarlyPreparationProfile:
    def __init__(
        self,
        *,
        enabled: bool,
        cpu_profile: bool = False,
        observe_memo: bool = True,
        cpu_selection: str = "current_evidence",
        observe_process_cpu: bool = False,
    ) -> None:
        if cpu_selection not in {"current_evidence", "message_inputs", "all_workers"}:
            raise ValueError("EARLY CPU selection must be current_evidence, message_inputs or all_workers")
        if cpu_profile and not enabled:
            raise ValueError("EARLY CPU capture requires selected-span observations")
        self.enabled = enabled
        self.cpu_profile = cpu_profile
        self.cpu_selection = cpu_selection
        self.observe_process_cpu = observe_process_cpu
        self.spans = EndPreparationProfile(
            enabled=enabled,
            cpu_profile=cpu_profile,
            cpu_selection="selected_scope" if cpu_profile and cpu_selection != "all_workers" else "all_workers",
        )
        self.clocks: list[dict[str, Any]] = []
        self.spans.clocks = self.clocks
        self._by_clock: dict[int, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._cpu_target_clock: int | None = None
        self._cpu_claimed = False
        self._cpu_selected_scope: dict[str, Any] | None = None
        self._binder_attempts: list[dict[str, Any]] = []
        if enabled and observe_memo:
            self.spans.text_memo = EndTextMemoObservation()
        self.case_outcome = {"status": "not_completed"}

    def _active(self):
        # The real SQL adapter deliberately enters a fresh Context carrying
        # only its memo. The original witness observer's native-thread record
        # remains the clock source across that boundary, as in span.begin().
        with self.spans._lock:
            worker = self.spans._workers.get(threading.get_native_id())
            identity = self.spans._clock.get() if worker is None else worker["clock"]
        with self._lock:
            clock = self._by_clock.get(identity)
            return clock is not None and not clock["finish_attempts"]

    def _sync(self, label, original):
        observed = self.spans.sync(label, original)

        @wraps(original)
        def call(*args, **kwargs):
            return observed(*args, **kwargs) if self._active() else original(*args, **kwargs)

        return call

    def _async(self, label, original, *, preparation_entry=False):
        observed = self.spans.asynchronous(label, original)

        @wraps(original)
        async def call(*args, **kwargs):
            if not self._active():
                return await original(*args, **kwargs)
            identity = self.spans._clock.get()
            error = None
            try:
                return await observed(*args, **kwargs)
            except BaseException as original_error:
                error = original_error
                raise
            finally:
                if preparation_entry and self.cpu_profile and identity == self._cpu_target_clock:
                    # A rejected precondition probe does not end the original
                    # preparation. Preserve each attempt without closing its
                    # clock's capture window; only actual finish does that.
                    with self._lock:
                        self._binder_attempts.append(
                            {
                                "clock": identity,
                                "end_ns": time.monotonic_ns(),
                                "label": label,
                                "kind": "binder_attempt_raised" if error is not None else "binder_attempt_returned",
                                "error": None if error is None else type(error).__name__,
                            }
                        )

        return call

    def _current_evidence_scope(self, original_scope, original_method, *, message_inputs=False):
        @contextmanager
        @wraps(original_scope)
        def selected(*args, **kwargs):
            # The original SQL adapter has cleared ContextVars here. Select by
            # the original witness's current native worker, once for the case.
            parent = current_v4_preparation_memo()
            with self.spans._lock, self._lock:
                worker = self.spans._workers.get(threading.get_native_id())
                clock = None if worker is None else self._by_clock.get(worker["clock"])
                eligible = (
                    self.cpu_profile
                    and worker is not None
                    and clock is not None
                    and worker["clock"] == self._cpu_target_clock
                    and parent is not None
                    and id(parent._v4_preparation_owner) == worker["clock"]
                    and not clock["finish_attempts"]
                    and not self._cpu_claimed
                )
                if eligible:
                    self._cpu_claimed = True
                    worker["cprofile_selected_method"] = {
                        "module": original_method.__module__,
                        "qualname": original_method.__qualname__,
                        "file": original_method.__code__.co_filename,
                        "line": original_method.__code__.co_firstlineno,
                    }
                    worker["cprofile_scope"] = "original_selected_method_body" if message_inputs else "original_current_evidence_scope"
                    worker["cprofile_memo_parent"] = id(parent)
            if not eligible:
                with original_scope(*args, **kwargs) as memo:
                    yield memo
                return
            label = "cprofile.message_inputs" if message_inputs else "cprofile.current_evidence_memo_scope"
            with self.spans.span(label) as scope:
                with self._lock:
                    worker["cprofile_method_span"] = scope["id"]
                    self._cpu_selected_scope = scope
                with self.spans._worker_cpu_capture(worker), original_scope(*args, **kwargs) as memo:
                    yield memo

        return selected

    def _message_inputs(self, original):
        selected_scope = self._current_evidence_scope(nullcontext, original, message_inputs=True)

        @wraps(original)
        def call(*args, **kwargs):
            with selected_scope():
                return original(*args, **kwargs)

        return call

    @contextmanager
    def install(self, monkeypatch, engine):
        # All diagnostic modes import the same boundaries before the original
        # clock begins. Timed work and all original ownership checks remain.
        from elspeth.web.composer import advisor_checkpoint as advisor
        from elspeth.web.composer import continuation_checkpoint_early_physical as physical
        from elspeth.web.composer import continuation_checkpoint_end_detached as detached
        from elspeth.web.composer import continuation_current_evidence as current_evidence
        from elspeth.web.composer import continuation_prompt_producers as prompt_producers
        from elspeth.web.composer import continuation_runtime as runtime
        from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence
        from elspeth.web.composer.continuation_selection import SelectedContinuationState
        from elspeth.web.sessions import continuation_p4_application_store as application_store
        from elspeth.web.sessions import continuation_runtime as sql_runtime
        from elspeth.web.sessions.service import SessionServiceImpl

        original_init = ContinuationActionClock.__init__
        original_finish = ContinuationActionClock.finish_preparation
        profile = self

        def initialize(clock, policy):
            original_init(clock, policy)
            cpu_start = _cpu_sample() if profile.observe_process_cpu else None
            row = {
                "identity": id(clock),
                "policy_identity": id(policy),
                "policy": policy.model_dump(mode="json"),
                "start_ns": round(clock.started_monotonic * 1_000_000_000),
                "preparation_deadline_monotonic": clock.preparation_deadline_monotonic,
                "action_deadline": clock.deadline,
                "thread": threading.get_native_id(),
                "thread_cpu_start_ns": time.thread_time_ns(),
                "finish_attempts": [],
            }
            if cpu_start is not None:
                row["cpu_samples_start"] = cpu_start
            with profile._lock:
                profile.clocks.append(row)
                profile._by_clock[id(clock)] = row
                if profile.cpu_profile and profile._cpu_target_clock is None:
                    profile._cpu_target_clock = id(clock)
                    profile.spans._end_clocks.add(id(clock))
            profile.spans._clock.set(id(clock))

        def finish(clock):
            entered = time.monotonic_ns()
            cpu_entry = _cpu_sample() if profile.observe_process_cpu else None
            error = None
            try:
                return original_finish(clock)
            except BaseException as original:
                error = original
                raise
            finally:
                ended, cpu = time.monotonic_ns(), time.thread_time_ns()
                with profile._lock:
                    row = profile._by_clock.get(id(clock))
                    if row is not None:
                        attempt = {
                            "entry_ns": entered,
                            "end_ns": ended,
                            "elapsed_to_entry_ns": entered - row["start_ns"],
                            "elapsed_to_exit_ns": ended - row["start_ns"],
                            "error": None if error is None else type(error).__name__,
                            "same_thread": row["thread"] == threading.get_native_id(),
                            "seen": sorted(clock._seen),
                        }
                        if attempt["same_thread"]:
                            attempt["thread_cpu_ns"] = cpu - row["thread_cpu_start_ns"]
                        if cpu_entry is not None:
                            attempt["cpu_samples_entry"] = cpu_entry
                            attempt["entry_process_cpu_ns"] = cpu_entry["process_ns"] - row["cpu_samples_start"]["process_ns"]
                            if attempt["same_thread"]:
                                attempt["entry_thread_cpu_ns"] = cpu_entry["thread_ns"] - row["cpu_samples_start"]["thread_ns"]
                        row["finish_attempts"].append(attempt)
                with profile.spans._lock:
                    profile.spans._closed_clocks.setdefault(id(clock), ended)

        token = self.spans._clock.set(None)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(ContinuationActionClock, "__init__", initialize)
                patch.setattr(ContinuationActionClock, "finish_preparation", finish)
                if self.enabled:
                    # Observe outer boundaries only. In particular, do not
                    # replace memo render/parser/hash dependencies: doing so
                    # would disable the very reuse being measured.
                    for module, name in (
                        (physical, "prepare_checkpoint_phase_owner"),
                        (physical, "capture_continuation_runtime_owners"),
                        (physical, "bind_checkpoint_early_physical_request"),
                        (detached, "restore_prepared_end_runtime"),
                        (detached, "capture_continuation_runtime_owners"),
                    ):
                        patch.setattr(
                            module,
                            name,
                            self._async(
                                f"{module.__name__}.{name}",
                                vars(module)[name],
                                preparation_entry=name == "bind_checkpoint_early_physical_request",
                            ),
                        )
                    if self.cpu_profile and self.cpu_selection == "current_evidence":
                        patch.setattr(
                            current_evidence,
                            "borrow_v4_preparation_memo_scope",
                            self._current_evidence_scope(
                                current_evidence.borrow_v4_preparation_memo_scope,
                                current_evidence._assemble_runtime_message_evidence,
                            ),
                        )
                    patch.setattr(runtime, "_capture_owners", self._sync("runtime._capture_owners", runtime._capture_owners))
                    for owner, name, original, label in (
                        (
                            sql_runtime,
                            "read_runtime_source_on_connection",
                            sql_runtime.read_runtime_source_on_connection,
                            "runtime.full_source_read",
                        ),
                        (
                            sql_runtime,
                            "load_selected_private_chunks_on_connection",
                            sql_runtime.load_selected_private_chunks_on_connection,
                            "runtime.sql_private_closure",
                        ),
                        (
                            AdmittedTurnEvidence,
                            "_require_original_graph",
                            AdmittedTurnEvidence._require_original_graph,
                            "evidence.original_graph",
                        ),
                        (
                            prompt_producers,
                            "_inputs_from_current_evidence_on_connection",
                            prompt_producers._inputs_from_current_evidence_on_connection,
                            "runtime.message_inputs",
                        ),
                        (
                            application_store,
                            "require_p4_application_journal_on_connection",
                            application_store.require_p4_application_journal_on_connection,
                            "evidence.sql_journal",
                        ),
                        (
                            SelectedContinuationState,
                            "__post_init__",
                            SelectedContinuationState.__post_init__,
                            "selection.private_closure",
                        ),
                    ):
                        if self.cpu_profile and self.cpu_selection == "message_inputs" and label == "runtime.message_inputs":
                            original = self._message_inputs(original)
                        patch.setattr(owner, name, self._sync(label, original))
                    patch.setattr(
                        advisor, "prepare_provider_input", self._sync("advisor.prepare_provider_input", advisor.prepare_provider_input)
                    )
                    owner = advisor.AdvisorCheckpointOwner
                    patch.setattr(owner, "_build_checkpoint_arguments", self._sync("advisor.arguments", owner._build_checkpoint_arguments))
                    patch.setattr(
                        owner, "_call_advisor_with_audit_body", self._async("advisor.physical_body", owner._call_advisor_with_audit_body)
                    )
                    patch.setattr(
                        SessionServiceImpl,
                        "prepare_v4_checkpoint_source_sync",
                        self._sync("sessions.prepare_v4_checkpoint_source_sync", SessionServiceImpl.prepare_v4_checkpoint_source_sync),
                    )
                with (
                    self.spans.text_memo.install(patch) if self.spans.text_memo is not None else nullcontext(),
                    self.spans.sql(engine),
                    self.spans.workers(patch),
                ):
                    yield self
        finally:
            self.spans._clock.reset(token)

    def snapshot(self) -> dict[str, Any]:
        result = self.spans.snapshot()
        with self._lock:
            result.update(
                observer="original_early_preparation",
                mode="inclusive_only" if not self.enabled else "spans" if self.spans.text_memo is not None else "spans_only",
                clocks=[{**row, "finish_attempts": [dict(attempt) for attempt in row["finish_attempts"]]} for row in self.clocks],
                case_outcome=dict(self.case_outcome),
                binder_attempts=[dict(attempt) for attempt in self._binder_attempts],
            )
            if self.cpu_profile:
                issues = [
                    issue.replace("original END", "original EARLY").replace("END-entry-exit", "EARLY-entry-exit")
                    for issue in result["cpu_capture_issues"]
                ]
                clock = None if self._cpu_target_clock is None else self._by_clock.get(self._cpu_target_clock)
                if self.cpu_selection != "all_workers":
                    scope = self._cpu_selected_scope
                    partial = scope is not None and (
                        "end_ns" not in scope or scope.get("error") is not None or clock is None or not clock["finish_attempts"]
                    )
                    if scope is None:
                        target = "message-inputs call" if self.cpu_selection == "message_inputs" else "current-evidence memo scope"
                        issues.append(f"CPU capture did not reach the original {target}")
                    if self.cpu_selection == "message_inputs" and scope is not None:
                        matches = [item for item in result["cpu_profiles"] if item["selected_method_span"] == scope["id"]]
                        if len(result["cpu_profiles"]) != 1 or len(matches) != 1:
                            issues.append("CPU capture does not match the first message-inputs call exactly once")
                        elif scope["clock"] != matches[0]["clock"] or scope["worker"] != matches[0]["worker_id"]:
                            issues.append("CPU message-inputs scope lost its original clock/worker pairing")
                else:
                    partial = clock is None or not clock["finish_attempts"]
                    captured = [profile["worker_id"] for profile in result["cpu_profiles"]]
                    if len(captured) != len(set(captured)) or set(captured) != set(result["cpu_expected_workers"]):
                        issues.append("CPU capture does not match every original EARLY worker exactly once")
                if partial:
                    issues.append("CPU capture contains only a partial original EARLY preparation workload")
                if result["incomplete_spans"] or result["unpaired_submissions"]:
                    issues.append("CPU capture has unfinished original worker observations")
                result.update(
                    cpu_capture_issues=issues,
                    cpu_capture_valid=not issues,
                    cpu_capture_status="partial_invalid" if partial else "invalid" if issues else "captured_preparation",
                    cpu_scope=(
                        "native thread CPU in first original message-inputs call; queue, gate and earlier work excluded; "
                        "event-loop frames not profiled; cProfile overhead included; no unprofiled wall-speed claim"
                        if self.cpu_selection == "message_inputs"
                        else "native thread CPU in first original current-evidence memo scope; queue and gate excluded; "
                        "cProfile overhead included; no unprofiled wall-speed claim"
                        if self.cpu_selection == "current_evidence"
                        else "native thread CPU in every original required worker; gate included, queue excluded; "
                        "event-loop frames not profiled; cProfile overhead included; no unprofiled wall-speed claim"
                    ),
                )
                if self.cpu_selection == "message_inputs":
                    result["cpu_selection"] = "message_inputs"
        return result

    def flush(self, path: Path):
        import json

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.snapshot(), indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            return error
        return None


@contextmanager
def early_preparation_profile(monkeypatch, engine, *, output_key):
    mode = os.environ.get(PROFILE_FLAG)
    if mode is None:
        yield None
        return
    assert mode in {"0", "1", "2"}
    cpu_mode = os.environ.get(CPU_PROFILE_FLAG)
    assert cpu_mode in {None, "current_evidence", "message_inputs", "all_workers"}, "unknown EARLY CPU target"
    assert cpu_mode is None or mode in {"1", "2"}, "EARLY CPU capture requires selected-span mode"
    root = Path(__file__).resolve().parents[2]
    directory = Path(os.environ.get(PROFILE_DIRECTORY, ".claude/lanes/early-preparation-profile"))
    if not directory.is_absolute():
        directory = root / directory
    directory = directory.resolve()
    assert directory.is_relative_to(root), "EARLY diagnostic output must remain inside this project"
    profile = EarlyPreparationProfile(
        enabled=mode in {"1", "2"},
        cpu_profile=cpu_mode is not None,
        observe_memo=mode == "1",
        cpu_selection=cpu_mode or "current_evidence",
    )
    try:
        with profile.install(monkeypatch, engine):
            yield profile
    except BaseException as original:
        profile.case_outcome = {"status": "raised", "error": type(original).__name__}
        raise
    else:
        profile.case_outcome = {"status": "returned"}
    finally:
        # Diagnostic output must never mask the original preparation failure.
        failure = profile.flush(directory / f"{output_key}.json")
        if failure is not None:
            profile.spans.errors.append("EARLY diagnostic output write failed: " + type(failure).__name__)


def _semantic_preparation_window(snapshot, target_clock):
    """Qualify native observations without treating post-finish work as preparation."""
    issues = list(snapshot["errors"])
    clocks = [row for row in snapshot["clocks"] if row["identity"] == target_clock]
    # A real driver constructs many short-lived clocks. Their object ids may
    # repeat; the selected constructor row, not an id alone, identifies ours.
    index = snapshot.get("target_clock_index")
    if index is not None:
        clocks = [row for ordinal, row in enumerate(snapshot["clocks"]) if ordinal == index and row["identity"] == target_clock]
    if len(clocks) != 1:
        return {"valid": False, "issues": [*issues, "semantic target needs one original constructed clock"]}
    clock = clocks[0]
    refusals = snapshot.get("refused_preparation_guards", [])
    endpoints = [*clock["finish_attempts"], *refusals]
    if len(endpoints) != 1:
        return {"valid": False, "issues": [*issues, "semantic target needs one original finish or refused guard endpoint"]}
    finish = endpoints[0]
    refused = bool(refusals)
    endpoint_worker = finish.get("worker") if refused else None
    start, end = clock["start_ns"], finish["entry_ns"]
    if end < start or (not refused and not finish["same_thread"]):
        issues.append("semantic clock has an invalid interval or changed finish thread")
    if refused and (
        finish["clock"] != target_clock
        or finish["error"] != "TimeoutError"
        or finish["seen"]
        or finish["same_thread"] != (finish["thread"] == clock["thread"])
    ):
        issues.append("semantic refusal did not belong to the original unfinished preparation")
    if snapshot["incomplete_spans"] or snapshot["unpaired_submissions"] or snapshot["pending_sql_contexts"]:
        issues.append("semantic observations have unfinished spans, workers or SQL")
    if snapshot["cpu_profile_enabled"] or snapshot["cpu_profiles"] or snapshot["text_memo"] is not None:
        issues.append("semantic observation must exclude CPU profiling and memo probes")
    records = snapshot["records"]
    by_id = {row["id"]: row for row in records}
    if refused and snapshot["enabled"] and not finish["same_thread"]:
        worker = by_id.get(endpoint_worker)
        if (
            worker is None
            or worker["kind"] != "worker"
            or worker["clock"] != target_clock
            or worker["thread"] != finish["thread"]
            or not start <= worker["start_ns"] <= end <= finish["end_ns"] <= worker.get("end_ns", -1)
        ):
            issues.append("semantic refused guard lost its original completed native worker")
    next_constructor = min(
        (row["start_ns"] for row in snapshot["clocks"] if row["identity"] == target_clock and row["start_ns"] > start),
        default=None,
    )
    selected = [
        row
        for row in records
        if row["clock"] == target_clock and row["start_ns"] >= start and (next_constructor is None or row["start_ns"] < next_constructor)
    ]
    submissions = [row for row in selected if row["kind"] == "submission" and start <= row["start_ns"] <= end]
    for submission in submissions:
        if submission.get("submission_failed"):
            continue
        worker = by_id.get(submission.get("worker_id"))
        if (
            worker is None
            or worker["clock"] != target_clock
            or worker["kind"] != "worker"
            or worker.get("submission") != submission["id"]
            or not start <= worker["start_ns"] <= end
            or "end_ns" not in worker
            or worker["end_ns"] < worker["start_ns"]
            or (worker["end_ns"] > end and worker["id"] != endpoint_worker)
        ):
            issues.append("semantic preparation submission lost its complete original worker window")
    contained = [row["id"] for row in selected if start <= row["start_ns"] <= row.get("end_ns", end + 1) <= end]
    crossing = [row["id"] for row in selected if row["start_ns"] < end < row.get("end_ns", end + 1)]
    after = [row["id"] for row in selected if row["start_ns"] >= end]
    if "cpu_samples_start" not in clock or "cpu_samples_entry" not in finish:
        issues.append("semantic preparation lacks its aligned process/thread CPU samples")
    return {
        "valid": not issues,
        "issues": issues,
        "clock": target_clock,
        "endpoint_kind": "refused_guard" if refused else "finish_preparation",
        "endpoint_worker": endpoint_worker,
        "endpoint_entry_ns": end,
        "endpoint_exit_ns": finish["end_ns"],
        "start_ns": start,
        "finish_entry_ns": end,
        "finish_exit_ns": finish["end_ns"],
        "elapsed_to_entry_ns": finish["elapsed_to_entry_ns"],
        "elapsed_to_exit_ns": finish["elapsed_to_exit_ns"],
        "headroom_at_entry_ns": round(clock["preparation_deadline_monotonic"] * 1_000_000_000) - end,
        "headroom_at_exit_ns": round(clock["preparation_deadline_monotonic"] * 1_000_000_000) - finish["end_ns"],
        "finish_error": finish["error"],
        "contained_record_ids": contained,
        "crossing_record_ids": crossing,
        "postfinish_record_ids": after,
        "preparation_submission_ids": [row["id"] for row in submissions],
    }


class SemanticPreparationProfile(EarlyPreparationProfile):
    """Observe one original semantic clock from a fixture or supported driver."""

    def __init__(self, *, enabled: bool) -> None:
        super().__init__(enabled=enabled, cpu_profile=False, observe_memo=False, observe_process_cpu=True)
        self.target_clock: int | None = None
        self.target_clock_index: int | None = None
        self.driver_operation: tuple[str, object] | None = None
        self.driver_selection: dict[str, Any] | None = None
        self.refused_preparation_guards: list[dict[str, Any]] = []
        self._finishing_clocks: set[int] = set()

    def select_clock(self, clock: ContinuationActionClock) -> None:
        if (
            type(clock) is not ContinuationActionClock
            or id(clock) not in self._by_clock
            or self.target_clock is not None
            or clock._seen
            or self._by_clock[id(clock)]["finish_attempts"]
        ):
            raise ValueError("semantic observation requires its one original constructed clock")
        self.target_clock = id(clock)
        self.target_clock_index = next(index for index, row in enumerate(self.clocks) if row is self._by_clock[id(clock)])
        with self.spans._lock:
            self.spans._closed_clocks.pop(id(clock), None)

    def bind_driver_operation(self, *, operation_id, session_id) -> None:
        if self.driver_operation is not None or self.target_clock is not None:
            raise ValueError("semantic observation already has an original target")
        self.driver_operation = operation_id, session_id

    def _select_driver_clock(self, *, clock, bound_clock, advisor_policy, operation_id, session_id, cursor, frame_kind, phase):
        if (
            self.driver_operation != (operation_id, session_id)
            or self.target_clock is not None
            or type(clock) is not ContinuationActionClock
            or clock is not bound_clock
            # The phase redecodes the retained policy; the original driver
            # clock keeps its original owner's equal policy instance.
            or clock.policy != advisor_policy
            or (frame_kind, phase) != ("compose", "action_ready")
        ):
            return
        if id(clock) not in self._by_clock or clock._seen or self._by_clock[id(clock)]["finish_attempts"]:
            self.spans.errors.append("semantic driver target lacks its original unfinished constructor observation")
            return
        self.select_clock(clock)
        self.driver_selection = {
            "operation_id": operation_id,
            "session_id": str(session_id),
            "cursor": cursor,
            "frame_kind": frame_kind,
            "phase": phase,
            "clock": id(clock),
            "thread": threading.get_native_id(),
        }

    def _active(self):
        with self.spans._lock:
            worker = self.spans._workers.get(threading.get_native_id())
            identity = self.spans._clock.get() if worker is None else worker["clock"]
        return (
            identity == self.target_clock
            and self.target_clock_index is not None
            and self._by_clock.get(identity) is self.clocks[self.target_clock_index]
            and not self.refused_preparation_guards
            and super()._active()
        )

    @contextmanager
    def install(self, monkeypatch, engine):
        # Import the same bindings in inclusive-only and selected-span modes,
        # before the actual clock. None is a hash/render/memo dependency.
        from elspeth.web.composer import continuation_prompt_producers as producers
        from elspeth.web.composer import continuation_runtime as runtime
        from elspeth.web.composer import continuation_semantic_advisor as semantic
        from elspeth.web.composer import continuation_semantic_advisor_phase as phase
        from elspeth.web.composer.continuation import ComposeFrame
        from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
        from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
        from elspeth.web.sessions import continuation_semantic_advisor as sql_semantic

        synchronous = (
            (phase, "select_semantic_advisor_graph_and_inputs_on_connection", phase.select_semantic_advisor_graph_and_inputs_on_connection),
            (phase, "capture_semantic_advisor_source", phase.capture_semantic_advisor_source),
            (phase, "make_semantic_advisor_invocation", phase.make_semantic_advisor_invocation),
            (phase, "read_semantic_advisor_retained_inputs_on_connection", phase.read_semantic_advisor_retained_inputs_on_connection),
            (phase, "prepare_semantic_advisor_request", phase.prepare_semantic_advisor_request),
            (phase, "prepare_semantic_advisor_on_connection", phase.prepare_semantic_advisor_on_connection),
            (sql_semantic, "read_runtime_source_on_connection", sql_semantic.read_runtime_source_on_connection),
            (semantic, "prepare_provider_input", semantic.prepare_provider_input),
            (semantic.PreparedSemanticAdvisorRequest, "verify", semantic.PreparedSemanticAdvisorRequest.verify),
            (CurrentContinuationMessageProducers, "restore_shelf", CurrentContinuationMessageProducers.restore_shelf),
            (
                CurrentContinuationMessageProducers,
                "_restore_shelf_for_semantic_request",
                CurrentContinuationMessageProducers._restore_shelf_for_semantic_request,
            ),
            (
                CurrentContinuationMessageProducers,
                "_require_current_with_tool_sources_on_connection",
                CurrentContinuationMessageProducers._require_current_with_tool_sources_on_connection,
            ),
            (CurrentContinuationMessageProducers, "_require_current", CurrentContinuationMessageProducers._require_current),
            (producers, "_inputs_with_tool_sources_on_connection", producers._inputs_with_tool_sources_on_connection),
        )
        asynchronous = (
            (ContinuationLivePhases, "current", ContinuationLivePhases.current),
            (runtime, "_restore_runtime", runtime._restore_runtime),
            (phase, "_run_semantic_advisor_preparation", phase._run_semantic_advisor_preparation),
            # The outcome owner imports this function locally on invocation.
            (semantic, "run_semantic_advisor", semantic.run_semantic_advisor),
        )
        with super().install(monkeypatch, engine), monkeypatch.context() as patch:
            original_guard = ContinuationActionClock.require_preparation_time
            original_finish = ContinuationActionClock.finish_preparation

            @wraps(original_finish)
            def finish(clock):
                self._finishing_clocks.add(id(clock))
                try:
                    return original_finish(clock)
                finally:
                    self._finishing_clocks.discard(id(clock))

            @wraps(original_guard)
            def guard(clock, *, monotonic_now=None):
                selected = (
                    id(clock) == self.target_clock
                    and self.target_clock_index is not None
                    and self._by_clock.get(id(clock)) is self.clocks[self.target_clock_index]
                    and not self.clocks[self.target_clock_index]["finish_attempts"]
                    and not self.refused_preparation_guards
                    and id(clock) not in self._finishing_clocks
                )
                if not selected:
                    return original_guard(clock, monotonic_now=monotonic_now)
                entered, cpu_entry = time.monotonic_ns(), _cpu_sample()
                try:
                    return original_guard(clock, monotonic_now=monotonic_now)
                except BaseException as original:
                    ended = time.monotonic_ns()
                    with self.spans._lock, self._lock:
                        row = self._by_clock[id(clock)]
                        worker = self.spans._workers.get(threading.get_native_id())
                        same_thread = row["thread"] == threading.get_native_id()
                        endpoint = {
                            "clock": id(clock),
                            "entry_ns": entered,
                            "end_ns": ended,
                            "elapsed_to_entry_ns": entered - row["start_ns"],
                            "elapsed_to_exit_ns": ended - row["start_ns"],
                            "error": type(original).__name__,
                            "same_thread": same_thread,
                            "thread": threading.get_native_id(),
                            "worker": None if worker is None else worker["id"],
                            "seen": sorted(clock._seen),
                            "cpu_samples_entry": cpu_entry,
                            "entry_process_cpu_ns": cpu_entry["process_ns"] - row["cpu_samples_start"]["process_ns"],
                        }
                        if same_thread:
                            endpoint["entry_thread_cpu_ns"] = cpu_entry["thread_ns"] - row["cpu_samples_start"]["thread_ns"]
                        if worker is not None:
                            endpoint["worker_cpu_to_entry_ns"] = cpu_entry["thread_ns"] - worker["thread_cpu_start_ns"]
                        self.refused_preparation_guards.append(endpoint)
                        self.spans._closed_clocks.setdefault(id(clock), ended)
                    raise

            patch.setattr(ContinuationActionClock, "finish_preparation", finish)
            patch.setattr(ContinuationActionClock, "require_preparation_time", guard)
            if self.enabled:
                for owner, name, original in synchronous:
                    patch.setattr(owner, name, self._sync(f"{owner.__name__}.{name}", original))
                for owner, name, original in asynchronous:
                    patch.setattr(owner, name, self._async(f"{owner.__name__}.{name}", original))
            if self.driver_operation is not None:
                original_preparation = phase._run_semantic_advisor_preparation

                @wraps(original_preparation)
                async def preparation(phases, selected, *, running, policy, clock):
                    frame = selected.stack.frames[-1]
                    if self.target_clock is None and type(frame) is ComposeFrame:
                        self._select_driver_clock(
                            clock=clock,
                            bound_clock=phases.action_clock,
                            advisor_policy=policy.actions.advisor,
                            operation_id=running.claim.operation_id,
                            session_id=running.claim.session_id,
                            cursor=selected.stack.cursor,
                            frame_kind=frame.frame_kind,
                            phase=frame.phase,
                        )
                    return await original_preparation(phases, selected, running=running, policy=policy, clock=clock)

                patch.setattr(phase, "_run_semantic_advisor_preparation", preparation)
            yield self

    def snapshot(self):
        result = super().snapshot()
        result.update(
            target_clock_index=self.target_clock_index,
            driver_selection=self.driver_selection,
            refused_preparation_guards=[dict(row) for row in self.refused_preparation_guards],
        )
        result.update(
            observer="original_initial_semantic_preparation",
            process_id=os.getpid(),
            preparation=_semantic_preparation_window(result, self.target_clock),
            process_cpu_scope="all threads in this process between post-constructor and original endpoint-entry CPU samples",
            thread_cpu_scope="event-loop thread only; async intervals include other activity; native workers are recorded separately",
            wall_scope="original clock anchor to forwarded finish/refused-guard entry/exit; inclusive spans overlap and must not be summed",
        )
        return result


@contextmanager
def semantic_preparation_profile(monkeypatch, engine, *, output_key, driver_operation=None):
    mode = os.environ.get(SEMANTIC_PROFILE_FLAG)
    if mode is None:
        yield None
        return
    assert mode in {"0", "2"}, "semantic timing supports inclusive-only 0 or spans-only 2"
    root = Path(__file__).resolve().parents[2]
    directory = Path(os.environ.get(SEMANTIC_PROFILE_DIRECTORY, ".claude/lanes/semantic-preparation-profile"))
    if not directory.is_absolute():
        directory = root / directory
    directory = directory.resolve()
    assert directory.is_relative_to(root), "semantic diagnostic output must remain inside this project"
    profile = SemanticPreparationProfile(enabled=mode == "2")
    if driver_operation is not None:
        operation_id, session_id = driver_operation
        profile.bind_driver_operation(operation_id=operation_id, session_id=session_id)
    try:
        with profile.install(monkeypatch, engine):
            yield profile
    except BaseException as original:
        profile.case_outcome = {"status": "raised", "error": type(original).__name__}
        raise
    else:
        profile.case_outcome = {"status": "returned"}
    finally:
        failure = profile.flush(directory / f"{output_key}.json")
        if failure is not None:
            profile.spans.errors.append("semantic diagnostic output write failed: " + type(failure).__name__)
