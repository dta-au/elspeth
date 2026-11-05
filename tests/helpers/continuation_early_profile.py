"""Opt-in EARLY timing at original clocks, source boundaries and SQL workers.

Mode zero observes only inclusive clock-to-preparation-finish time. Mode one
adds selected inclusive spans, native thread CPU, SQL and the already calibrated
original-witness observer, plus four-family memo statistics. Mode two records
the same spans without the memo observer. A separate CPU opt-in, available
in either span mode, captures the first current-evidence memo scope. No mode
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


class EarlyPreparationProfile:
    def __init__(self, *, enabled: bool, cpu_profile: bool = False, observe_memo: bool = True) -> None:
        if cpu_profile and not enabled:
            raise ValueError("EARLY CPU capture requires selected-span observations")
        self.enabled = enabled
        self.cpu_profile = cpu_profile
        self.spans = EndPreparationProfile(
            enabled=enabled, cpu_profile=cpu_profile, cpu_selection="selected_scope" if cpu_profile else "all_workers"
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

    def _current_evidence_scope(self, original_scope, original_method):
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
                    worker["cprofile_scope"] = "original_current_evidence_scope"
                    worker["cprofile_memo_parent"] = id(parent)
            if not eligible:
                with original_scope(*args, **kwargs) as memo:
                    yield memo
                return
            with self.spans.span("cprofile.current_evidence_memo_scope") as scope:
                with self._lock:
                    worker["cprofile_method_span"] = scope["id"]
                    self._cpu_selected_scope = scope
                with self.spans._worker_cpu_capture(worker), original_scope(*args, **kwargs) as memo:
                    yield memo

        return selected

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
            with profile._lock:
                profile.clocks.append(row)
                profile._by_clock[id(clock)] = row
                if profile.cpu_profile and profile._cpu_target_clock is None:
                    profile._cpu_target_clock = id(clock)
                    profile.spans._end_clocks.add(id(clock))
            profile.spans._clock.set(id(clock))

        def finish(clock):
            entered = time.monotonic_ns()
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
                    if self.cpu_profile:
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
                scope = self._cpu_selected_scope
                clock = None if self._cpu_target_clock is None else self._by_clock.get(self._cpu_target_clock)
                partial = scope is not None and (
                    "end_ns" not in scope or scope.get("error") is not None or clock is None or not clock["finish_attempts"]
                )
                if scope is None:
                    issues.append("CPU capture did not reach the original current-evidence memo scope")
                elif partial:
                    issues.append("CPU capture contains only a partial original EARLY preparation workload")
                if result["incomplete_spans"] or result["unpaired_submissions"]:
                    issues.append("CPU capture has unfinished original worker observations")
                result.update(
                    cpu_capture_issues=issues,
                    cpu_capture_valid=not issues,
                    cpu_capture_status="partial_invalid" if partial else "invalid" if issues else "captured_preparation",
                    cpu_scope="native thread CPU in first original current-evidence memo scope; queue and gate excluded; "
                    "cProfile overhead included; no unprofiled wall-speed claim",
                )
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
    assert cpu_mode in {None, "current_evidence"}, "EARLY CPU target must be current_evidence"
    assert cpu_mode is None or mode in {"1", "2"}, "EARLY CPU capture requires selected-span mode"
    root = Path(__file__).resolve().parents[2]
    directory = Path(os.environ.get(PROFILE_DIRECTORY, ".claude/lanes/early-preparation-profile"))
    if not directory.is_absolute():
        directory = root / directory
    directory = directory.resolve()
    assert directory.is_relative_to(root), "EARLY diagnostic output must remain inside this project"
    profile = EarlyPreparationProfile(enabled=mode in {"1", "2"}, cpu_profile=cpu_mode == "current_evidence", observe_memo=mode == "1")
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
