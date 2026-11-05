"""Opt-in EARLY timing at original clocks, source boundaries and SQL workers.

Mode zero observes only inclusive clock-to-preparation-finish time. Mode one
adds selected inclusive spans, native thread CPU, SQL and the already calibrated
original-witness observer. Neither mode replaces submitted callables, adjusts a
deadline, consumes a clock stage, caches a result or records private payloads.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from elspeth.web.composer.continuation_clock import ContinuationActionClock
from tests.helpers.continuation_end_profile import EndPreparationProfile

PROFILE_FLAG = "ELSPETH_TEST_EARLY_PREPARATION_PROFILE"
PROFILE_DIRECTORY = "ELSPETH_TEST_EARLY_PREPARATION_PROFILE_DIRECTORY"


class EarlyPreparationProfile:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.spans = EndPreparationProfile(enabled=enabled)
        self.clocks: list[dict[str, Any]] = []
        self._by_clock: dict[int, dict[str, Any]] = {}
        self._lock = threading.RLock()
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

    def _async(self, label, original):
        observed = self.spans.asynchronous(label, original)

        @wraps(original)
        async def call(*args, **kwargs):
            return await observed(*args, **kwargs) if self._active() else await original(*args, **kwargs)

        return call

    @contextmanager
    def install(self, monkeypatch, engine):
        # Both diagnostic modes import the same boundaries before the original
        # clock begins. Timed work and all original ownership checks remain.
        from elspeth.web.composer import advisor_checkpoint as advisor
        from elspeth.web.composer import continuation_checkpoint_early_physical as physical
        from elspeth.web.composer import continuation_checkpoint_end_detached as detached
        from elspeth.web.composer import continuation_runtime as runtime
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
                        patch.setattr(module, name, self._async(f"{module.__name__}.{name}", vars(module)[name]))
                    patch.setattr(runtime, "_capture_owners", self._sync("runtime._capture_owners", runtime._capture_owners))
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
                with self.spans.sql(engine), self.spans.workers(patch):
                    yield self
        finally:
            self.spans._clock.reset(token)

    def snapshot(self) -> dict[str, Any]:
        result = self.spans.snapshot()
        with self._lock:
            result.update(
                observer="original_early_preparation",
                mode="spans" if self.enabled else "inclusive_only",
                clocks=[{**row, "finish_attempts": [dict(attempt) for attempt in row["finish_attempts"]]} for row in self.clocks],
                case_outcome=dict(self.case_outcome),
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
    assert mode in {"0", "1"}
    root = Path(__file__).resolve().parents[2]
    directory = Path(os.environ.get(PROFILE_DIRECTORY, ".claude/lanes/early-preparation-profile"))
    if not directory.is_absolute():
        directory = root / directory
    directory = directory.resolve()
    assert directory.is_relative_to(root), "EARLY diagnostic output must remain inside this project"
    profile = EarlyPreparationProfile(enabled=mode == "1")
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
