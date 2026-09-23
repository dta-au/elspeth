"""A collector group's FAILED verdict is one transaction and final on resume (elspeth-5887fb7928 CODEX-R2).

Codex's review of 3d3cc1e18 found the collector failure path was not
crash-atomic. The flush state was completed FAILED, then each member's
accept-time hold in its own transaction, then the settle seam wrote the
member terminals and released the journal rows. Two windows followed:

- crash after the FAILED flush state but before any hold: resume re-flushed
  and a non-deterministic plugin turned the recorded failure into a success;
- crash after the first hold: restore took the group for closed
  (``completed_at IS NOT NULL``), dropped its still-BLOCKED members as
  post-closure residuals, and every resume after that stranded them.

Now ``complete_collector_failure`` writes the flush state and every member
hold FAILED in ONE leader-fenced transaction, so a FAILED
``CollectorGroupFailure`` hold IS the recorded verdict. Resume completes a
recorded verdict (terminals, escalation, journal release) from those holds
WITHOUT re-invoking the plugin: crash timing must not change the outcome
(the C4 ruling's collector twin). A crash inside the verdict transaction
leaves no verdict, and the group is re-flushed, exactly as an aggregation
flush with no recorded verdict is.

Shape (Codex's reduced one-group probe): one source row, then an EOF
batch_replicate passthrough buffer so the scope's flush runs after the
source is exhausted (resume refuses an incomplete source), then json_explode
(three members), then a batch_stats collector (require_all), then a sink.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from elspeth.contracts import Determinism, PipelineRow
from elspeth.contracts.enums import NodeStateStatus, RunStatus, TerminalPath
from elspeth.contracts.plugin_context import PluginContext
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.execution.node_states import NodeStateRepository
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import node_states_table, token_work_items_table
from elspeth.engine.executors.collector import CollectorExecutor
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.batch_stats import BatchStats
from tests.integration.pipeline.test_barrier_hold_payload import build_pipeline, resume_pipeline, run_pipeline, terminal_counts

_COLLECTOR_PIPELINE = """
aggregations:
  - name: eof_buffer
    plugin: batch_replicate
    input: buffered
    on_success: rows
    on_error: discard
    trigger: {{count: 100}}
    output_mode: passthrough
    options:
      include_copy_index: false
      schema: {{mode: observed}}
transforms:
  - name: explode
    plugin: json_explode
    input: rows
    on_success: pages
    on_error: discard
    options:
      array_field: items
      output_field: item
      schema: {{mode: observed}}
collectors:
  - name: page_stitcher
    plugin: batch_stats
    input: pages
    on_success: out
    options:
      value_field: item
      schema: {{mode: observed}}
scopes:
  - name: document_pages
    opener: explode
    closer: page_stitcher
    policy: require_all
"""

_DOCS = [{"id": 1, "items": [3, 1, 2]}]
# Two canonical integers whose sum is not canonical (beyond 2**53): the
# UNMODIFIED shipped batch_stats fails its group on every call (C5).
_OVERFLOW_DOCS = [{"id": 1, "items": [5000000000000000, 5000000000000000]}]

_REAL_BATCH_STATS_PROCESS = BatchStats.process
_REAL_COMPLETE_COLLECTOR_FAILURE = ExecutionRepository.complete_collector_failure
_REAL_COMPLETE_NODE_STATES_FAILED_MANY = NodeStateRepository.complete_node_states_failed_many
_REAL_RECORD_TOKEN_OUTCOME_LEADER = DataFlowRepository.record_token_outcome_leader
_REAL_MARK_BLOCKED_BARRIER_TERMINAL = TokenSchedulerRepository.mark_blocked_barrier_terminal


class _Crash(Exception):
    """A process death at one instant: an ordinary exception the engine does not convert."""


def _flip_first_call(monkeypatch: pytest.MonkeyPatch, mode: str) -> list[list[Any]]:
    """batch_stats fails its FIRST flush (returned error, or a non-canonical sum), then works."""
    monkeypatch.setattr(BatchStats, "determinism", Determinism.NON_DETERMINISTIC)
    calls: list[list[Any]] = []

    def process(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        calls.append([row["item"] for row in rows])
        result = _REAL_BATCH_STATS_PROCESS(self, rows, ctx)
        if len(calls) > 1:
            return result
        if mode == "returned_error":
            return TransformResult.error({"reason": "deliberate_failure"}, retryable=False)
        assert result.row is not None
        return TransformResult.success(
            PipelineRow(dict(result.row.to_dict(), sum=1152921504606859321), result.row.contract),
            success_reason=result.success_reason,
        )

    monkeypatch.setattr(BatchStats, "process", process)
    return calls


def _count_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[Any]]:
    calls: list[list[Any]] = []

    def process(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        calls.append([row["item"] for row in rows])
        return _REAL_BATCH_STATS_PROCESS(self, rows, ctx)

    monkeypatch.setattr(BatchStats, "process", process)
    return calls


def _inject(monkeypatch: pytest.MonkeyPatch, window: str) -> list[str]:
    """Arm one crash window; the returned list records that the crash really fired."""
    fired: list[str] = []

    if window == "inside_verdict":
        in_verdict: list[bool] = []

        def complete_collector_failure(self: ExecutionRepository, **kwargs: Any) -> None:
            in_verdict.append(True)
            try:
                _REAL_COMPLETE_COLLECTOR_FAILURE(self, **kwargs)
            finally:
                in_verdict.clear()

        def complete_node_states_failed_many(self: NodeStateRepository, *args: Any, **kwargs: Any) -> None:
            _REAL_COMPLETE_NODE_STATES_FAILED_MANY(self, *args, **kwargs)
            if in_verdict:
                fired.append(window)
                raise _Crash("inside the verdict transaction, after its state writes executed")

        monkeypatch.setattr(ExecutionRepository, "complete_collector_failure", complete_collector_failure)
        monkeypatch.setattr(NodeStateRepository, "complete_node_states_failed_many", complete_node_states_failed_many)
    elif window == "after_verdict":

        def close_failed_group(self: CollectorExecutor, *args: Any, **kwargs: Any) -> Any:
            # Every arm reaches this only after its verdict transaction committed.
            fired.append(window)
            raise _Crash("after the verdict committed, before its disposition")

        monkeypatch.setattr(CollectorExecutor, "_close_failed_group", close_failed_group)
    elif window == "after_first_terminal":

        def record_token_outcome_leader(self: DataFlowRepository, **kwargs: Any) -> Any:
            result = _REAL_RECORD_TOKEN_OUTCOME_LEADER(self, **kwargs)
            if kwargs["path"] is TerminalPath.UNROUTED and not fired:
                fired.append(window)
                raise _Crash("after the first member terminal")
            return result

        monkeypatch.setattr(DataFlowRepository, "record_token_outcome_leader", record_token_outcome_leader)
    elif window == "before_release":

        def mark_blocked_barrier_terminal(self: TokenSchedulerRepository, **kwargs: Any) -> Any:
            if kwargs["barrier_key"].startswith("collector:") and not fired:
                fired.append(window)
                raise _Crash("after every member terminal, before the journal release")
            return _REAL_MARK_BLOCKED_BARRIER_TERMINAL(self, **kwargs)

        monkeypatch.setattr(TokenSchedulerRepository, "mark_blocked_barrier_terminal", mark_blocked_barrier_terminal)
    else:
        raise AssertionError(f"unknown window {window!r}")
    return fired


def _journal_statuses(env: dict[str, Any]) -> list[tuple[str, int]]:
    with env["db"].connection() as conn:
        return sorted(
            (str(status), int(count))
            for status, count in conn.execute(
                select(token_work_items_table.c.status, func.count()).group_by(token_work_items_table.c.status)
            ).all()
        )


def _collector_hold_statuses(env: dict[str, Any]) -> list[str]:
    """Statuses of the collector node's states on the three members (not the opener's flush state)."""
    with env["db"].connection() as conn:
        rows = conn.execute(
            select(node_states_table.c.status, node_states_table.c.error_json).where(node_states_table.c.node_id.like("collector_%"))
        ).all()
    return sorted(str(status) for status, error_json in rows if error_json is None or "CollectorGroupFailure" in error_json)


@pytest.mark.parametrize("mode", ["returned_error", "noncanonical"])
@pytest.mark.parametrize("window", ["after_verdict", "after_first_terminal", "before_release"])
def test_a_recorded_collector_verdict_is_completed_on_resume_without_the_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, window: str
) -> None:
    control_env = build_pipeline(tmp_path / "control", _COLLECTOR_PIPELINE, _DOCS)
    control_calls = _flip_first_call(monkeypatch, mode)
    control = run_pipeline(control_env)
    assert control.status is RunStatus.FAILED
    assert len(control_calls) == 1

    env = build_pipeline(tmp_path / "crashed", _COLLECTOR_PIPELINE, _DOCS)
    calls = _flip_first_call(monkeypatch, mode)
    with monkeypatch.context() as crash_patch:
        fired = _inject(crash_patch, window)
        with pytest.raises(_Crash):
            run_pipeline(env)
    assert fired == [window], "the injected crash must actually fire"
    # The verdict is durable whole: all three holds FAILED, none OPEN.
    assert _collector_hold_statuses(env) == [NodeStateStatus.FAILED.value] * 3
    assert ("blocked", 3) in _journal_statuses(env)

    resumed = resume_pipeline(env)

    assert len(calls) == 1, "a recorded collector failure must not re-invoke the plugin"
    assert resumed.status is RunStatus.FAILED
    assert terminal_counts(env["db"]) == terminal_counts(control_env["db"])
    assert _journal_statuses(env) == _journal_statuses(control_env)
    assert not env["output_path"].exists()


@pytest.mark.parametrize("mode", ["returned_error", "noncanonical"])
def test_a_crash_inside_the_verdict_leaves_no_verdict_and_the_group_is_re_flushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, _DOCS)
    calls = _flip_first_call(monkeypatch, mode)
    with monkeypatch.context() as crash_patch:
        fired = _inject(crash_patch, "inside_verdict")
        with pytest.raises(_Crash):
            run_pipeline(env)
    assert fired == ["inside_verdict"]
    # Nothing of the verdict survived: every member hold is still OPEN.
    assert _collector_hold_statuses(env) == [NodeStateStatus.OPEN.value] * 3

    resumed = resume_pipeline(env)

    # No verdict was recorded, so the flush re-runs; this plugin succeeds the second time.
    assert len(calls) == 2
    assert calls[1] == calls[0]
    assert resumed.status is RunStatus.COMPLETED
    assert env["output_path"].read_text().count("\n") == 1


def test_the_shipped_overflow_failure_survives_two_crashed_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex's shipped reproducer: UNMODIFIED batch_stats, a non-canonical sum, a crash after the verdict.

    Before the fix the crash left both members BLOCKED through every later
    resume ("did not converge within 1008 intake/flush rounds"). A second
    crash, in the resumed disposition this time, must not lose them either.
    """
    control_env = build_pipeline(tmp_path / "control", _COLLECTOR_PIPELINE, _OVERFLOW_DOCS)
    _count_calls(monkeypatch)
    assert run_pipeline(control_env).status is RunStatus.FAILED

    env = build_pipeline(tmp_path / "crashed", _COLLECTOR_PIPELINE, _OVERFLOW_DOCS)
    calls = _count_calls(monkeypatch)
    with monkeypatch.context() as crash_patch:
        fired = _inject(crash_patch, "after_verdict")
        with pytest.raises(_Crash):
            run_pipeline(env)
    assert fired == ["after_verdict"]
    with monkeypatch.context() as crash_patch:
        fired = _inject(crash_patch, "before_release")
        with pytest.raises(_Crash):
            resume_pipeline(env)
    assert fired == ["before_release"]

    resumed = resume_pipeline(env)

    assert len(calls) == 1
    assert resumed.status is RunStatus.FAILED
    assert terminal_counts(env["db"]) == terminal_counts(control_env["db"])
    assert _journal_statuses(env) == _journal_statuses(control_env)


def test_a_collector_verdict_whose_acknowledgement_is_lost_reports_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verdict committed, then its call raised before returning (Codex minor's collector twin).

    The flush guard's cleanup reads the durable state back (FAILED) instead of
    reporting "audit trail has permanent OPEN state", and resume completes
    the recorded verdict without the plugin.
    """
    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, _DOCS)
    calls = _flip_first_call(monkeypatch, "returned_error")
    committed: list[bool] = []

    def committed_then_acknowledgement_lost(self: ExecutionRepository, **kwargs: Any) -> None:
        _REAL_COMPLETE_COLLECTOR_FAILURE(self, **kwargs)
        committed.append(True)
        raise _Crash("committed but acknowledgement lost")

    with monkeypatch.context() as lost_ack:
        lost_ack.setattr(ExecutionRepository, "complete_collector_failure", committed_then_acknowledgement_lost)
        with pytest.raises(_Crash, match="committed but acknowledgement lost"):
            run_pipeline(env)
    assert committed == [True]
    assert _collector_hold_statuses(env) == [NodeStateStatus.FAILED.value] * 3

    resumed = resume_pipeline(env)

    assert len(calls) == 1
    assert resumed.status is RunStatus.FAILED
    assert ("terminal", 5) in _journal_statuses(env)


@pytest.mark.parametrize("tamper", ["one_hold_reopened", "hold_reason_stripped"])
def test_a_tampered_recorded_verdict_is_refused_not_re_flushed_or_stranded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    """The verdict is one transaction, so restore proves it whole before completing it.

    A group whose held members only partly carry the verdict, or carry it
    without its reason and disposition, is audit corruption. Resume refuses it
    rather than re-flushing some members or stranding the rest.
    """
    from sqlalchemy import update

    from elspeth.contracts.errors import AuditIntegrityError

    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, _DOCS)
    calls = _flip_first_call(monkeypatch, "returned_error")
    with monkeypatch.context() as crash_patch:
        _inject(crash_patch, "after_verdict")
        with pytest.raises(_Crash):
            run_pipeline(env)
    with env["db"].connection() as conn:
        hold_id = conn.execute(
            select(node_states_table.c.state_id)
            .where(node_states_table.c.node_id.like("collector_%"))
            .where(node_states_table.c.error_json.like('%"CollectorGroupFailure"%'))
            .order_by(node_states_table.c.state_id)
            .limit(1)
        ).scalar_one()
    with env["db"].engine.begin() as conn:
        if tamper == "one_hold_reopened":
            values: dict[str, Any] = {"status": NodeStateStatus.OPEN.value, "completed_at": None, "error_json": None, "duration_ms": None}
            match = "carry a group-failure verdict"
        else:
            values = {"error_json": '{"exception":"x","phase":"collector_flush","type":"CollectorGroupFailure"}'}
            match = "does not carry the verdict's failure_reason"
        conn.execute(update(node_states_table).where(node_states_table.c.state_id == hold_id).values(**values))

    with pytest.raises(AuditIntegrityError, match=match):
        resume_pipeline(env)
    assert len(calls) == 1
