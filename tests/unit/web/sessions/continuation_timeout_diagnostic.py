"""Temporary payload-free observation of the actual G1 timeout owners."""

import sys
import threading
import time
from collections import deque
from contextvars import ContextVar
from pathlib import Path
from traceback import extract_tb
from types import CoroutineType, GeneratorType

import httpx
from sqlalchemy import select

from elspeth.web.composer import continuation_application_graph as application_graph_owner
from elspeth.web.composer import continuation_audit_buffer as recorder_owner
from elspeth.web.composer import continuation_live_phases as phase_owner
from elspeth.web.composer import continuation_prompt_producers as prompt_owner
from elspeth.web.composer import continuation_runtime as runtime_owner
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from elspeth.web.sessions import continuation_runtime as sql_runtime_owner
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.composer_operations import ComposerOperationCancel
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_driver import _SegmentDriver
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.schemas import ComposerOperationSnapshot


def _error(error):
    if isinstance(error, BaseExceptionGroup):
        return tuple(item for child in error.exceptions for item in _error(child))
    message = str(error).splitlines()[0] if str(error) else "<empty>"
    try:
        require_no_credential_material(message, surface="test_g1_timeout_diagnostic")
    except CredentialMaterialRefused:
        message = "<credential material omitted>"
    return ((type(error).__name__, message),)


def _error_identity(error, seen=None):
    if error is None:
        return None
    visited = set() if seen is None else seen
    if id(error) in visited:
        return {"id": id(error), "cycle": True}
    visited.add(id(error))
    return {
        "id": id(error),
        "leaves": _error(error),
        "cancel_reasons": tuple(arg.reason.value for arg in error.args if type(arg) is ComposerOperationCancel),
        "children": tuple(_error_identity(child, visited) for child in error.exceptions) if isinstance(error, BaseExceptionGroup) else (),
        "cause": _error_identity(error.__cause__, visited),
    }


def _driver_clocks(driver):
    now = time.monotonic()
    result = {"monotonic": now}
    _section(
        result,
        "deadlines",
        lambda: {
            "sequence": driver.running.claim.segment_sequence,
            "generation": driver.running.claim.ownership_generation,
            "hard_remaining": driver.sql_deadline.hard_expires_at - now,
            "action_remaining": driver.sql_deadline.remaining_action(monotonic_now=now),
            "settling": driver.sql_deadline._settling,
        },
    )
    return result


def _task(task):
    if task is None:
        return None
    frames = []
    current = task.get_coro()
    for _ in range(24):
        if isinstance(current, CoroutineType):
            frame, following = current.cr_frame, current.cr_await
        elif isinstance(current, GeneratorType):
            frame, following = current.gi_frame, current.gi_yieldfrom
        else:
            break
        if frame is not None:
            frames.append((Path(frame.f_code.co_filename).name, frame.f_code.co_name, frame.f_lineno))
        current = following
    result = {"name": task.get_name(), "done": task.done(), "cancelled": task.cancelled(), "waiter": tuple(frames)}
    if task.done() and not task.cancelled():
        failure = task.exception()
        result["error"] = None if failure is None else _error(failure)
    return result


def _capture_error(error):
    return {
        "capture_error": _error(error),
        "frames": tuple((Path(frame.filename).name, frame.name, frame.lineno) for frame in extract_tb(error.__traceback__)),
    }


def _section(result, key, observe):
    try:
        result[key] = observe()
    except Exception as original:
        result[key] = _capture_error(original)


class G1TimeoutProbe:
    def __init__(self, monkeypatch, app, sdk, session_id, operation_id):
        self.app, self.sdk = app, sdk
        self.session_id, self.operation_id = session_id, operation_id
        self.last_public = None
        self.last_http_status = None
        self.phases = []
        self.runtime_capture = None
        self.phase_sql_reads = []
        self._phase_read = ContextVar("g1_timeout_probe_phase_read", default=None)
        self.originals: list[BaseException] = []
        self.watch_events = deque(maxlen=64)
        self.settlement_events = deque(maxlen=32)
        worker = app.state.composer_async_worker
        target = f"/api/sessions/{session_id}/operations/{operation_id}"
        get, advance = httpx.AsyncClient.get, _SegmentDriver.advance
        watch_stage, settle, failure_read = _SegmentDriver.watch_stage, _SegmentDriver.settle, _SegmentDriver.read_failure
        job_finished, loop_finished = ComposerAsyncWorker._job_finished, ComposerAsyncWorker._loop_finished
        registered_read, capture_owners = runtime_owner._registered_read, runtime_owner._capture_owners
        phase_registered_read = phase_owner._registered_read
        read_runtime_source = sql_runtime_owner.read_runtime_source_on_connection
        read_current_inputs = sql_runtime_owner._read_current_inputs
        finish_runtime_source = sql_runtime_owner._runtime_source_from_inputs_on_connection
        read_evidence = prompt_owner.read_turn_evidence_on_connection
        project_inputs = prompt_owner._inputs_from_current_evidence_on_connection
        recorder_json = recorder_owner.recorder_snapshot_json
        read_runtime = worker._authority.read_continuation_runtime

        async def observe_phase_registered_read(work, owner, operation):
            if work.running.claim.operation_id != operation_id or owner not in (
                sql_runtime_owner.ContinuationRuntimeSource,
                runtime_owner.RestoredContinuationRuntime,
            ):
                return await phase_registered_read(work, owner, operation)
            observation = {
                "owner": owner.__name__,
                "submit": time.monotonic(),
                "physical_start": None,
                "physical_end": None,
                "await_end": None,
                "thread_id": None,
                "boundary": "queued",
                "after_runtime_capture": self.runtime_capture is not None and self.runtime_capture["complete"],
                "source_spans": [],
                "recorder_encode_count": 0,
                "recorder_encode_bytes": 0,
                "recorder_encode_max_bytes": 0,
                "recorder_encode_seconds": 0.0,
            }
            self.phase_sql_reads.append(observation)

            def physical():
                observation["thread_id"] = threading.get_ident()
                observation["physical_start"] = time.monotonic()
                observation["boundary"] = "physical-running"
                token = self._phase_read.set(observation)
                try:
                    result = operation()
                except BaseException:
                    observation["boundary"] = "physical-raised"
                    raise
                else:
                    observation["boundary"] = "physical-returned"
                    return result
                finally:
                    observation["physical_end"] = time.monotonic()
                    self._phase_read.reset(token)

            try:
                return await phase_registered_read(work, owner, physical)
            finally:
                observation["await_end"] = time.monotonic()

        def observe_sql_span(name, original, *args, **kwargs):
            observation = self._phase_read.get()
            if observation is None:
                return original(*args, **kwargs)
            started = time.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                observation["source_spans"].append((name, started, time.monotonic()))

        def observe_runtime_source(*args, **kwargs):
            return observe_sql_span("read_runtime_source", read_runtime_source, *args, **kwargs)

        def observe_current_inputs(*args, **kwargs):
            return observe_sql_span("read_current_inputs", read_current_inputs, *args, **kwargs)

        def observe_finish_source(*args, **kwargs):
            return observe_sql_span("finish_runtime_source", finish_runtime_source, *args, **kwargs)

        def observe_evidence(*args, **kwargs):
            return observe_sql_span("read_turn_evidence", read_evidence, *args, **kwargs)

        def observe_project_inputs(*args, **kwargs):
            return observe_sql_span("project_inputs", project_inputs, *args, **kwargs)

        def observe_recorder_json(snapshot):
            observation = self._phase_read.get()
            if observation is None:
                return recorder_json(snapshot)
            started = time.monotonic()
            canonical = recorder_json(snapshot)
            byte_length = len(canonical.encode("utf-8"))
            observation["recorder_encode_count"] += 1
            observation["recorder_encode_bytes"] += byte_length
            observation["recorder_encode_max_bytes"] = max(observation["recorder_encode_max_bytes"], byte_length)
            observation["recorder_encode_seconds"] += time.monotonic() - started
            return canonical

        async def observe_registered_read(work, owner, operation):
            if owner is not runtime_owner.CapturedContinuationRuntime:
                return await registered_read(work, owner, operation)

            def observe_physical_capture():
                observation = {
                    "thread_id": threading.get_ident(),
                    "boundary": "start",
                    "read_count": 0,
                    "complete": False,
                }
                self.runtime_capture = observation
                try:
                    result = operation()
                except BaseException:
                    observation["boundary"] = "raised"
                    raise
                else:
                    observation["boundary"] = "returned"
                    return result
                finally:
                    observation["complete"] = True

            return await registered_read(work, owner, observe_physical_capture)

        def observe_runtime_read(*args, **kwargs):
            observation = self.runtime_capture
            if observation is not None and not observation["complete"] and observation["thread_id"] == threading.get_ident():
                observation["read_count"] += 1
                observation["boundary"] = "initial-current-read" if observation["read_count"] == 1 else "final-current-read"
            return read_runtime(*args, **kwargs)

        def observe_capture_owners(*args, **kwargs):
            observation = self.runtime_capture
            if observation is not None and not observation["complete"] and observation["thread_id"] == threading.get_ident():
                observation["boundary"] = "current-capture"
            return capture_owners(*args, **kwargs)

        async def observe_get(client, url, *args, **kwargs):
            response = await get(client, url, *args, **kwargs)
            if str(url) == target:
                self.last_http_status = response.status_code
                if response.status_code == 200:
                    snapshot = ComposerOperationSnapshot.model_validate_json(response.content, strict=True)
                    self.last_public = snapshot.model_dump(
                        mode="json",
                        include={"status", "phase", "status_revision", "observed_at", "cancel_requested", "segment"},
                    )
            return response

        async def observe_advance(owner, stored, selected):
            if owner.running.claim.operation_id != operation_id:
                return await advance(owner, stored, selected)
            projection = selected.stack.frames[-1].model_dump(include={"frame_kind", "phase", "ingress_phase"})
            self.phases.append(("start", stored.cursor, projection))
            try:
                result = await advance(owner, stored, selected)
            except BaseException as original:
                self.originals.append(original)
                self.phases.append(("raised", stored.cursor, type(original).__name__))
                raise
            self.phases.append(("return", stored.cursor, projection))
            return result

        async def observe_watch(owner, stage, reader):
            if owner.running.claim.operation_id != operation_id:
                return await watch_stage(owner, stage, reader)
            self.watch_events.append(("watch_started", _driver_clocks(owner), _task(stage)))
            actual_read = type(reader).read

            async def observe_read(current_reader):
                try:
                    current, now = await actual_read(current_reader)
                except BaseException as original:
                    if current_reader is reader:
                        self.watch_events.append(("read_raised", _driver_clocks(owner), _error_identity(original)))
                    raise
                if current_reader is reader:
                    self.watch_events.append(
                        (
                            "read",
                            _driver_clocks(owner),
                            None
                            if current is None
                            else (current.attempt, current.current_segment_sequence, current.ownership_generation, current.status),
                            current is not None and current.cancel_requested_at is not None,
                            owner.worker._local_cancels[(session_id, operation_id)].is_set(),
                            now.isoformat(),
                        )
                    )
                return current, now

            with monkeypatch.context() as observation:
                observation.setattr(type(reader), "read", observe_read)
                try:
                    return await watch_stage(owner, stage, reader)
                except BaseException as original:
                    self.watch_events.append(("watch_raised", _driver_clocks(owner), _error_identity(original)))
                    raise
                finally:
                    self.watch_events.append(("watch_finished", _driver_clocks(owner), _task(stage)))

        async def observe_failure_read(owner, names):
            value = await failure_read(owner, names)
            if owner.running.claim.operation_id == operation_id:
                self.settlement_events.append(("actual_source", _driver_clocks(owner), type(value.source).__name__))
            return value

        async def observe_settle(owner, record, original=None, *, expired_failure=None):
            if owner.running.claim.operation_id != operation_id:
                return await settle(owner, record, original, expired_failure=expired_failure)
            self.settlement_events.append(
                (
                    "entered",
                    _driver_clocks(owner),
                    _error_identity(original),
                    record.cancel_requested_at is not None,
                    None if expired_failure is None else expired_failure.failure_code,
                )
            )
            try:
                result = await settle(owner, record, original, expired_failure=expired_failure)
            except BaseException as failure:
                self.settlement_events.append(("raised", _driver_clocks(owner), _error_identity(failure)))
                raise
            self.settlement_events.append(("returned", _driver_clocks(owner), type(result).__name__))
            return result

        def observe_job(owner, key, task):
            if owner is worker and key == (session_id, operation_id) and not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    self.originals.append(failure)
            return job_finished(owner, key, task)

        def observe_loop(owner, task):
            if owner is worker and not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    self.originals.append(failure)
            return loop_finished(owner, task)

        monkeypatch.setattr(httpx.AsyncClient, "get", observe_get)
        monkeypatch.setattr(_SegmentDriver, "advance", observe_advance)
        monkeypatch.setattr(_SegmentDriver, "watch_stage", observe_watch)
        monkeypatch.setattr(_SegmentDriver, "settle", observe_settle)
        monkeypatch.setattr(_SegmentDriver, "read_failure", observe_failure_read)
        monkeypatch.setattr(ComposerAsyncWorker, "_job_finished", observe_job)
        monkeypatch.setattr(ComposerAsyncWorker, "_loop_finished", observe_loop)
        monkeypatch.setattr(runtime_owner, "_registered_read", observe_registered_read)
        monkeypatch.setattr(runtime_owner, "_capture_owners", observe_capture_owners)
        monkeypatch.setattr(worker._authority, "read_continuation_runtime", observe_runtime_read)
        monkeypatch.setattr(phase_owner, "_registered_read", observe_phase_registered_read)
        monkeypatch.setattr(sql_runtime_owner, "read_runtime_source_on_connection", observe_runtime_source)
        monkeypatch.setattr(sql_runtime_owner, "_read_current_inputs", observe_current_inputs)
        monkeypatch.setattr(sql_runtime_owner, "_runtime_source_from_inputs_on_connection", observe_finish_source)
        monkeypatch.setattr(prompt_owner, "read_turn_evidence_on_connection", observe_evidence)
        monkeypatch.setattr(prompt_owner, "_inputs_from_current_evidence_on_connection", observe_project_inputs)
        monkeypatch.setattr(recorder_owner, "recorder_snapshot_json", observe_recorder_json)
        monkeypatch.setattr(application_graph_owner, "recorder_snapshot_json", observe_recorder_json)

    def _physical_runtime_capture(self):
        observation = self.runtime_capture
        if observation is None:
            return None
        result = dict(observation)
        if observation["complete"]:
            return result
        frame = sys._current_frames().get(observation["thread_id"])
        frames = []
        while frame is not None and len(frames) < 40:
            frames.append((Path(frame.f_code.co_filename).name, frame.f_code.co_name, frame.f_lineno))
            frame = frame.f_back
        if observation["complete"] or self.runtime_capture is not observation:
            result["observation_raced_completion"] = True
        else:
            result["physical_frames"] = tuple(frames)
        return result

    def _physical_phase_sql_reads(self):
        readings = []
        for observation in tuple(self.phase_sql_reads[-16:]):
            result = dict(observation)
            result["source_spans"] = tuple(
                (name, round(ended - started, 6), round(started - observation["submit"], 6))
                for name, started, ended in tuple(observation["source_spans"])
            )
            started = observation["physical_start"]
            ended = observation["physical_end"]
            awaited = observation["await_end"]
            result["queue_seconds"] = None if started is None else round(started - observation["submit"], 6)
            result["physical_seconds"] = None if started is None else round((ended or time.monotonic()) - started, 6)
            result["delivery_seconds"] = None if awaited is None else round(awaited - (ended or observation["submit"]), 6)
            if started is not None and ended is None:
                frame = sys._current_frames().get(observation["thread_id"])
                frames = []
                while frame is not None and len(frames) < 48:
                    frames.append((Path(frame.f_code.co_filename).name, frame.f_code.co_name, frame.f_lineno))
                    frame = frame.f_back
                if observation["physical_end"] is None:
                    result["physical_frames"] = tuple(frames)
                else:
                    result["observation_raced_completion"] = True
            readings.append(result)
        return {"total_reads": len(self.phase_sql_reads), "recent_reads": tuple(readings)}

    def capture(self):
        worker = self.app.state.composer_async_worker
        key = (self.session_id, self.operation_id)
        result = {
            "observed_monotonic": time.monotonic(),
            "last_public": self.last_public,
            "last_http_status": self.last_http_status,
            "sdk_roles": tuple(request["role"] for request in self.sdk.requests),
            "remaining_sdk_roles": tuple(role for role, _payload in self.sdk.steps),
            "phase_boundaries": tuple(self.phases),
            "original_leaves": tuple(item for original in self.originals for item in _error(original)),
            "original_identities": tuple(_error_identity(original) for original in self.originals),
            "watch_events": tuple(self.watch_events),
            "settlement_events": tuple(self.settlement_events),
        }
        _section(result, "runtime_capture", self._physical_runtime_capture)
        _section(result, "phase_sql_reads", self._physical_phase_sql_reads)
        _section(result, "job", lambda: _task(worker._jobs.get(key)))
        _section(result, "loop", lambda: _task(worker._loop_task))
        coordinator = worker._required_coordinators.get(key)
        if coordinator is not None:

            def tickets():
                return tuple(
                    {
                        "source": ticket.key.source.name,
                        "order": ticket.key.order,
                        "complete": ticket.complete,
                        "errors": tuple(item for error in ticket.errors for item in _error(error)),
                        "future": None
                        if ticket._future is None
                        else {
                            "done": ticket._future.done(),
                            "running": ticket._future.running(),
                            "cancelled": ticket._future.cancelled(),
                        },
                    }
                    for ticket in coordinator.tickets
                )

            _section(result, "tickets", tickets)
            timing = coordinator.sql_deadline_owner
            if timing is not None:
                now = float(time.monotonic())
                _section(
                    result,
                    "sql_timing",
                    lambda: {
                        "sequence": timing.running.claim.segment_sequence,
                        "generation": timing.running.claim.ownership_generation,
                        "hard_remaining": timing.hard_expires_at - now,
                        "action_remaining": timing.remaining_action(monotonic_now=now),
                        "settling": timing._settling,
                    },
                )
        adoption = worker._continuation_adoptions.get(key)
        if adoption is not None and adoption.lease is not None:
            lease = adoption.lease
            _section(result, "lease_tasks", lambda: tuple(_task(task) for task in tuple(lease._owned_tasks)))
            _section(result, "lease_close", lambda: _task(lease._close_task))
        try:
            with self.app.state.session_engine.connect() as conn:
                scope = (jobs.c.session_id == str(self.session_id), jobs.c.operation_id == self.operation_id)
                result["parent"] = tuple(
                    dict(row)
                    for row in conn.execute(
                        select(
                            jobs.c.status,
                            jobs.c.phase,
                            jobs.c.status_revision,
                            jobs.c.attempt,
                            jobs.c.current_segment_sequence,
                            jobs.c.ownership_generation,
                            jobs.c.checkpoint_id,
                        ).where(*scope)
                    ).mappings()
                )
                result["segments"] = tuple(
                    dict(row)
                    for row in conn.execute(
                        select(
                            segments.c.sequence,
                            segments.c.generation,
                            segments.c.state,
                            segments.c.checkpoint_id,
                            segments.c.claim_token.is_not(None).label("has_claim"),
                            segments.c.deadline_at,
                            segments.c.claim_expires_at,
                            segments.c.rollover_reason,
                        ).where(segments.c.session_id == str(self.session_id), segments.c.operation_id == self.operation_id)
                    ).mappings()
                )
                result["effects"] = tuple(
                    dict(row)
                    for row in conn.execute(
                        select(
                            effects.c.effect_key,
                            effects.c.owner,
                            effects.c.disposition,
                            effects.c.creator_sequence,
                            effects.c.creator_generation,
                            effects.c.receipt_key.is_not(None).label("has_receipt"),
                        ).where(effects.c.session_id == str(self.session_id), effects.c.operation_id == self.operation_id)
                    ).mappings()
                )
                stored = load_snapshot(conn, session_id=str(self.session_id), operation_id=self.operation_id)
                root = decode_continuation_snapshot(stored.payload_json)
                result["checkpoint"] = {
                    "cursor": root.cursor,
                    "frames": tuple(
                        frame.model_dump(
                            mode="json",
                            include={
                                "frame_kind",
                                "phase",
                                "ingress_phase",
                                "next_tool_index",
                                "provider_effect_key",
                                "slice_sequence",
                            },
                        )
                        for frame in root.frames
                    ),
                }
        except Exception as original:
            result["diagnostic_read_error"] = _capture_error(original)
        result["finished_monotonic"] = time.monotonic()
        return result
