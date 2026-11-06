"""Generic worker failures retain bounded diagnostics and their native originals."""

import asyncio
from uuid import uuid4

import pytest
from structlog import DropEvent
from structlog.testing import capture_logs

from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.web.composer.continuation import ComposeFrame, InitializeFrame
from elspeth.web.composer.provider_cost import CommonProviderCostRefused
from elspeth.web.sessions import continuation_driver as driver_module
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from tests.helpers.continuation_app import ScriptedSDK, continuation_app, effects, install_sdk, submit_and_settle

_SECRET = "private-driver-diagnostic-marker-93175"


class _UnavailableDiagnostics:
    def debug(self, *_args, **_kwargs):
        raise OSError(_SECRET)

    def error(self, *_args, **_kwargs):
        raise OSError(_SECRET)


def _info_threshold(_logger, method, event):
    if method == "debug":
        raise DropEvent
    return event


def _native_errors(root):
    pending, seen, result = [root], set(), []
    while pending:
        error = pending.pop()
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        result.append(error)
        pending.extend((error.__cause__, error.__context__))
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
    return result


def _assert_diagnostic(event, *, operation_id, expected_class):
    assert event["operation_id"] == operation_id and event["stage"] == "compose"
    nodes, edges = event["fault_nodes"], event["fault_edges"]
    assert type(nodes) is tuple and 1 <= len(nodes) <= 8
    assert type(edges) is tuple and len(edges) <= 16
    assert type(event["diagnostic_truncation"]) is tuple
    assert event["fault_classification"]
    assert nodes[0][0] == expected_class
    # The injected raise is outside the installed package. The diagnostic must
    # locate its real driver caller, not leak the arbitrary external filename.
    assert nodes[0][1] == "elspeth/web/sessions/continuation_driver.py"
    assert type(nodes[0][2]) is int and nodes[0][2] > 0
    assert all(type(node) is tuple and len(node) == 3 for node in nodes)
    assert all(0 <= source < len(nodes) and 0 <= target < len(nodes) for source, target, _relation in edges)
    assert not ({"exc_info", "exception", "error", "traceback", "args", "locals"} & event.keys())
    assert _SECRET not in repr(event) and __file__ not in repr(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("settled-generic-failure", "later-cleanup-failure", "sink-unavailable"))
async def test_real_worker_preserves_generic_failure_diagnostics(tmp_path, monkeypatch, outcome):
    cleanup_fails = outcome == "later-cleanup-failure"
    sink_unavailable = outcome == "sink-unavailable"
    operation_id = str(uuid4())
    original = RuntimeError(_SECRET)
    cause = ValueError(_SECRET)
    cleanup = LookupError(_SECRET)
    sdk = ScriptedSDK([])
    install_sdk(monkeypatch, sdk)
    driver_type = driver_module._SegmentDriver
    advance = driver_type.advance
    settle = driver_type.settle
    close_progress = driver_type.close_progress
    job_finished = ComposerAsyncWorker._job_finished
    verify_terminal = ComposerAsyncWorker._verify_terminal_before_release
    predecessors, reached, settlements, tasks, cleanup_calls = [], [], [], [], []
    verified, diagnostic_order = [], []

    def capture_after_verify(logger, method, event):
        event = _info_threshold(logger, method, event)
        if event["event"] == "composer_operation.stage_failure" and event["operation_id"] == operation_id:
            diagnostic_order.append(bool(verified))
        return event

    async def observe_verified(worker, running, *args, **kwargs):
        result = await verify_terminal(worker, running, *args, **kwargs)
        if running.claim.operation_id == operation_id:
            verified.append(result)
        return result

    async def fail_before_primary(driver, stored, selected):
        if driver.running.claim.operation_id != operation_id:
            return await advance(driver, stored, selected)
        top = selected.stack.frames[-1]
        if type(top) is InitializeFrame:
            predecessors.append(top.ingress_phase)
            return await advance(driver, stored, selected)
        assert type(top) is ComposeFrame and top.phase == "action_ready"
        assert predecessors and "reserved" in predecessors
        assert driver.phases.failure_custody is None
        assert not selected.stack.effect_frontier
        assert sdk.requests == []
        reached.append(driver)
        raise original from cause

    async def observe_settlement(driver, record, native=None, *, expired_failure=None):
        if driver.running.claim.operation_id == operation_id:
            assert native is original and expired_failure is None
            assert diagnostic_order == [], "ERROR must wait for actual terminal settlement and verification"
            result = await settle(driver, record, native, expired_failure=expired_failure)
            assert verified
            assert diagnostic_order == ([] if sink_unavailable else [True])
            settlements.append(result)
            return result
        return await settle(driver, record, native, expired_failure=expired_failure)

    async def close_then_fail(driver, *, retain_failure=False):
        await close_progress(driver, retain_failure=retain_failure)
        if driver.running.claim.operation_id == operation_id and cleanup_fails:
            assert settlements, "cleanup failure must follow successful generic failure settlement"
            cleanup_calls.append(driver)
            raise cleanup

    def observe_job_finished(worker, key, task):
        if key[1] == operation_id:
            assert not task.cancelled()
            tasks.append(task.exception())
        job_finished(worker, key, task)

    monkeypatch.setattr(driver_type, "advance", fail_before_primary)
    monkeypatch.setattr(driver_type, "settle", observe_settlement)
    monkeypatch.setattr(driver_type, "close_progress", close_then_fail)
    monkeypatch.setattr(ComposerAsyncWorker, "_job_finished", observe_job_finished)
    monkeypatch.setattr(ComposerAsyncWorker, "_verify_terminal_before_release", observe_verified)
    if sink_unavailable:
        monkeypatch.setattr(driver_module, "slog", _UnavailableDiagnostics())
    with capture_logs(processors=[capture_after_verify]) as logs:
        async with continuation_app(tmp_path, monkeypatch) as harness:
            snapshot = await submit_and_settle(harness, {"content": "Answer plainly", "operation_id": operation_id, "state_id": None})
            assert snapshot.status == "failed"
            assert _SECRET not in snapshot.model_dump_json()
            assert len(reached) == len(settlements) == len(tasks) == 1
            assert sdk.requests == [] and effects(harness.app, operation_id) == ()
            saved = harness.app.state.composer_async_operation_authority.get(session_id=harness.session_id, operation_id=operation_id)
            assert saved is not None and saved.status == "failed" and saved.result_json is not None
            assert _SECRET not in saved.result_json
            assert reached[0].lease.closed
            reached[0].work.coordinator.assert_completed()

    assert _SECRET not in repr(logs)
    stage_events = [event for event in logs if event["event"] == "composer_operation.stage_failure" and event["log_level"] == "error"]
    cleanup_events = [event for event in logs if event["event"] == "composer_operation.cleanup_failure" and event["log_level"] == "error"]
    if sink_unavailable:
        assert stage_events == [] and diagnostic_order == []
        assert original.__notes__
        assert set(original.__notes__) == {"Continuation structural diagnostic emission unavailable"}
    else:
        assert len(stage_events) == 1
        assert diagnostic_order == [True]
        _assert_diagnostic(stage_events[0], operation_id=operation_id, expected_class="RuntimeError")
        nodes = stage_events[0]["fault_nodes"]
        cause_index = next(index for index, node in enumerate(nodes) if node[0] == "ValueError")
        assert (0, cause_index, "cause") in stage_events[0]["fault_edges"]
    assert original.__cause__ is cause
    if cleanup_fails:
        assert len(cleanup_calls) == len(cleanup_events) == 1
        _assert_diagnostic(cleanup_events[0], operation_id=operation_id, expected_class="LookupError")
        native = _native_errors(tasks[0])
        assert any(error is original for error in native)
        assert any(error is cause for error in native)
        assert any(error is cleanup for error in native)
    else:
        assert tasks == [None] and cleanup_calls == cleanup_events == []
        assert any(error is original for error in reached[0]._consumed_native)


@pytest.mark.parametrize("shape", ("wide", "deep", "cycle", "external-filename"))
def test_structural_diagnostic_bounds_and_external_filename_redaction(shape):
    original = RuntimeError(_SECRET)
    if shape == "wide":
        original = ExceptionGroup(_SECRET, [RuntimeError(_SECRET) for _ in range(20)])
    elif shape == "deep":
        current = original
        for _ in range(12):
            child = RuntimeError(_SECRET)
            current.__cause__ = child
            current = child
    elif shape == "cycle":
        original.__cause__ = original
    else:
        # An arbitrary traceback filename is input, not an installed source
        # location. No filesystem artifact or application code is generated.
        try:
            exec(compile("raise original", f"/external/{_SECRET}.py", "exec"), {"original": original})
        except RuntimeError as raised:
            assert raised is original

    with capture_logs() as logs:
        driver_module._log_continuation_failure(
            "composer_operation.stage_failure", operation_id="bounded-diagnostic", stage="compose", error=original, audit_unavailable=True
        )
    assert len(logs) == 1
    event = logs[0]
    assert event["fault_classification"] == "unexpected" and event["log_level"] == "error"
    nodes, edges = event["fault_nodes"], event["fault_edges"]
    assert type(nodes) is tuple and 1 <= len(nodes) <= 8
    assert type(edges) is tuple and len(edges) <= 16
    assert all(0 <= source < len(nodes) and 0 <= target < len(nodes) for source, target, _relation in edges)
    assert _SECRET not in repr(logs)
    assert all(location is None and line is None for _class_name, location, line in nodes)
    if shape in ("wide", "deep"):
        assert event["diagnostic_truncation"], "bounded omission must be explicit"
    if shape == "cycle":
        assert len(nodes) == 1 and (0, 0, "cause") in edges


@pytest.mark.parametrize("malformed_notes", (False, True), ids=("ordinary-notes", "malformed-notes"))
def test_diagnostic_sink_failure_keeps_original_and_fixed_nonsecret_note(monkeypatch, malformed_notes):
    original = RuntimeError(_SECRET)
    cause = ValueError(_SECRET)
    original.__cause__ = cause
    if malformed_notes:
        original.__notes__ = ("original malformed notes",)
    monkeypatch.setattr(driver_module, "slog", _UnavailableDiagnostics())
    driver_module._log_continuation_failure(
        "composer_operation.stage_failure", operation_id="failed-diagnostic-sink", stage="compose", error=original, audit_unavailable=True
    )
    assert original.__cause__ is cause
    if malformed_notes:
        assert original.__notes__ == ("original malformed notes",)
    else:
        assert original.__notes__ == ["Continuation structural diagnostic emission unavailable"]
    assert _SECRET not in repr(original.__notes__)


@pytest.mark.parametrize(
    "outcome",
    (
        "cancellation",
        "deadline",
        "refusal",
        "two-deadlines",
        "mixed-controls",
        "repeated-members",
        "truncated-controls",
        "refusal-context",
        "deadline-cause",
    ),
)
def test_expected_control_outcomes_do_not_emit_error_when_audit_unavailable(outcome):
    if outcome == "cancellation":
        original = asyncio.CancelledError(_SECRET)
    elif outcome == "deadline":
        original = TimeoutError(_SECRET)
    elif outcome in ("refusal", "refusal-context"):
        original = CommonProviderCostRefused("COST_CAP_EXCEEDED", call_id=_SECRET)
        if outcome == "refusal-context":
            original.__context__ = RuntimeError(_SECRET)
    elif outcome == "deadline-cause":
        original = TimeoutError(_SECRET)
        original.__cause__ = RuntimeError(_SECRET)
    elif outcome == "two-deadlines":
        original = ExceptionGroup(_SECRET, [TimeoutError(_SECRET), TimeoutError(_SECRET)])
    elif outcome == "mixed-controls":
        original = BaseExceptionGroup(
            _SECRET,
            [asyncio.CancelledError(_SECRET), TimeoutError(_SECRET), CommonProviderCostRefused("COST_CAP_EXCEEDED", call_id=_SECRET)],
        )
    elif outcome == "repeated-members":
        repeated = TimeoutError(_SECRET)
        original = ExceptionGroup(_SECRET, [repeated, repeated, repeated])
    else:
        original = ExceptionGroup(_SECRET, [TimeoutError(_SECRET) for _ in range(20)])
    failure_type = "operation_failed" if isinstance(original, BaseExceptionGroup) else None
    with capture_logs() as all_logs:
        driver_module._log_continuation_failure(
            "composer_operation.stage_failure",
            operation_id="expected-control",
            stage="compose",
            error=original,
            audit_unavailable=True,
            failure_type=failure_type,
        )
    assert len(all_logs) == 1 and all_logs[0]["log_level"] == "debug"
    assert all_logs[0]["fault_classification"] == ("unknown" if outcome == "truncated-controls" else "expected")
    if outcome == "truncated-controls":
        assert all_logs[0]["diagnostic_truncation"]
    assert _SECRET not in repr(all_logs)
    with capture_logs(processors=[_info_threshold]) as visible:
        driver_module._log_continuation_failure(
            "composer_operation.stage_failure",
            operation_id="expected-control",
            stage="compose",
            error=original,
            audit_unavailable=True,
            failure_type=failure_type,
        )
    assert visible == []


def test_special_settlement_wrapper_cycle_is_bounded_and_preserves_original():
    original = ComposerOwnedSettlementFailure()
    cause = TimeoutError(_SECRET)
    original.__cause__ = cause
    cause.__cause__ = original
    with capture_logs() as logs:
        driver_module._log_continuation_failure(
            "composer_operation.stage_failure",
            operation_id="special-wrapper-cycle",
            stage="compose",
            error=original,
            audit_unavailable=True,
        )
    assert original.__cause__ is cause and cause.__cause__ is original
    assert len(logs) == 1
    event = logs[0]
    assert len(event["fault_nodes"]) == 2 and len(event["fault_edges"]) <= 16
    assert (0, 1, "cause") in event["fault_edges"] and (1, 0, "cause") in event["fault_edges"]
    assert event["fault_classification"] == "unknown" and event["log_level"] == "debug"
    assert _SECRET not in repr(logs)


def test_cancellation_audit_integrity_companion_remains_visible():
    original = asyncio.CancelledError(_SECRET)
    cause = AuditIntegrityError(_SECRET)
    original.__cause__ = cause
    with capture_logs() as logs:
        driver_module._log_continuation_failure(
            "composer_operation.stage_failure", operation_id="audit-companion", stage="compose", error=original, audit_unavailable=True
        )
    assert original.__cause__ is cause
    assert len(logs) == 1 and logs[0]["fault_classification"] == "unexpected" and logs[0]["log_level"] == "error"
    assert (0, 1, "cause") in logs[0]["fault_edges"]
    assert _SECRET not in repr(logs)


@pytest.mark.parametrize("failure_type", (None, "operation_failed"))
def test_diagnostic_uses_only_computed_outcome_for_unknown_class(monkeypatch, failure_type):
    class OriginalFailure(Exception):
        pass

    original = OriginalFailure(_SECRET)

    def refuse_reprojection(*_args, **_kwargs):
        raise AssertionError("diagnostics must not reproject the original graph")

    monkeypatch.setattr(driver_module, "project_composer_operation_error", refuse_reprojection)
    with capture_logs() as logs:
        driver_module._log_continuation_failure(
            "composer_operation.stage_failure",
            operation_id="computed-outcome",
            stage="compose",
            error=original,
            audit_unavailable=True,
            failure_type=failure_type,
        )
    assert len(logs) == 1
    assert logs[0]["fault_classification"] == ("unknown" if failure_type is None else "unexpected")
    assert logs[0]["log_level"] == ("debug" if failure_type is None else "error")
    assert logs[0]["fault_nodes"][0][0] == "OriginalFailure"
    assert _SECRET not in repr(logs)
