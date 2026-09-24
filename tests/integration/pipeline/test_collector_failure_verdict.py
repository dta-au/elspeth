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
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import collector_group_failures_table, node_states_table, token_work_items_table
from elspeth.engine.executors.collector import CollectorExecutor, CollectorOutcome
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.batch_stats import BatchStats
from elspeth.plugins.transforms.json_explode import JSONExplode
from elspeth.web.execution.accounting import load_run_accounting_from_db
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


@pytest.mark.parametrize("policy, expected_failures", [("require_all", 1), ("best_effort", 0)])
def test_zero_member_scope_records_only_structural_group_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str, expected_failures: int
) -> None:
    """A conforming opener's zero-row success closes its bound group without a collector flush."""
    pipeline = _COLLECTOR_PIPELINE.replace("    policy: require_all", f"    policy: {policy}")
    env = build_pipeline(tmp_path, pipeline, [{"id": 1, "items": []}])

    def emit_empty(self: JSONExplode, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        assert row["items"] == ()
        return TransformResult.success_empty(success_reason={"action": "transformed"})

    def collector_must_not_run(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        pytest.fail("zero-member collector group must never invoke the plugin")

    # Model an installed custom opener that declares filter semantics for its
    # zero-row arm. Shipped expanders route empty input as row errors instead.
    monkeypatch.setattr(JSONExplode, "passes_through_input", True)
    monkeypatch.setattr(JSONExplode, "can_drop_rows", True)
    monkeypatch.setattr(JSONExplode, "process", emit_empty)
    monkeypatch.setattr(BatchStats, "process", collector_must_not_run)

    result = run_pipeline(env)

    assert result.collector_groups_failed == expected_failures
    assert result.rows_failed == 0
    assert _collector_group_failure_count(env) == expected_failures
    assert load_run_accounting_from_db(env["db"], landscape_run_id=result.run_id).collector_groups_failed == expected_failures
    assert result.status is (RunStatus.COMPLETED_WITH_FAILURES if expected_failures else RunStatus.COMPLETED)
    require_all_openers = tuple(
        str(node_id) for node_id, binding in env["graph"].get_group_bindings().by_opener_node().items() if binding.policy == "require_all"
    )
    assert (
        RecorderFactory(env["db"]).barrier_restore.pending_empty_expansion_groups(run_id=result.run_id, opener_node_ids=require_all_openers)
        == ()
    )


def test_leader_intake_closes_a_zero_member_group_minted_without_leader_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The durable zero-member row alone lets the next leader intake close a follower's group."""
    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, [{"id": 1, "items": []}])
    real_notify = CollectorExecutor.notify_empty_group
    calls: list[str] = []

    def emit_empty(self: JSONExplode, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        return TransformResult.success_empty(success_reason={"action": "transformed"})

    def defer_opener_notification(self: CollectorExecutor, collector_name: str, group_id: str, ctx: PluginContext) -> CollectorOutcome:
        calls.append(group_id)
        if len(calls) == 1:
            # Models a follower, which mints with membership authority and has
            # no CollectorExecutor or leader coordination token to notify.
            return CollectorOutcome(held=False, collector_name=collector_name, group_id=group_id)
        return real_notify(self, collector_name, group_id, ctx)

    monkeypatch.setattr(JSONExplode, "passes_through_input", True)
    monkeypatch.setattr(JSONExplode, "can_drop_rows", True)
    monkeypatch.setattr(JSONExplode, "process", emit_empty)
    monkeypatch.setattr(CollectorExecutor, "notify_empty_group", defer_opener_notification)

    result = run_pipeline(env)

    assert len(calls) == 2
    assert calls[1] == calls[0]
    assert result.status is RunStatus.COMPLETED_WITH_FAILURES
    assert result.rows_failed == 0
    assert result.collector_groups_failed == 1
    assert _collector_group_failure_count(env) == 1


def test_zero_member_group_pending_at_crash_is_closed_on_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An opener does not rerun merely to rescue its empty group's missing leader verdict."""
    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, [{"id": 1, "items": []}])
    real_notify = CollectorExecutor.notify_empty_group
    opener_calls: list[bool] = []
    notify_calls: list[str] = []

    def emit_empty(self: JSONExplode, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        opener_calls.append(True)
        return TransformResult.success_empty(success_reason={"action": "transformed"})

    def crash_before_sweep_verdict(self: CollectorExecutor, collector_name: str, group_id: str, ctx: PluginContext) -> CollectorOutcome:
        notify_calls.append(group_id)
        if len(notify_calls) == 1:
            return CollectorOutcome(held=False, collector_name=collector_name, group_id=group_id)
        if len(notify_calls) == 2:
            raise _Crash("after empty group mint, before leader verdict")
        return real_notify(self, collector_name, group_id, ctx)

    monkeypatch.setattr(JSONExplode, "passes_through_input", True)
    monkeypatch.setattr(JSONExplode, "can_drop_rows", True)
    monkeypatch.setattr(JSONExplode, "process", emit_empty)
    monkeypatch.setattr(CollectorExecutor, "notify_empty_group", crash_before_sweep_verdict)

    with pytest.raises(_Crash, match="before leader verdict"):
        run_pipeline(env)
    assert _collector_group_failure_count(env) == 0
    with env["db"].connection() as conn:
        run_id = str(conn.execute(select(node_states_table.c.run_id).limit(1)).scalar_one())
    opener_node_ids = tuple(str(node_id) for node_id in env["graph"].get_group_bindings().by_opener_node())
    pending_before_resume = RecorderFactory(env["db"]).barrier_restore.pending_empty_expansion_groups(
        run_id=run_id, opener_node_ids=opener_node_ids
    )
    assert len(pending_before_resume) == 1

    resumed = resume_pipeline(env)

    assert len(opener_calls) == 1
    assert resumed.status is RunStatus.COMPLETED_WITH_FAILURES
    assert resumed.rows_failed == 0
    assert resumed.collector_groups_failed == 1
    assert len(notify_calls) == 3
    assert len(set(notify_calls)) == 1
    assert _collector_group_failure_count(env) == 1
    assert RecorderFactory(env["db"]).barrier_restore.pending_empty_expansion_groups(run_id=run_id, opener_node_ids=opener_node_ids) == ()


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


def _collector_group_failure_count(env: dict[str, Any]) -> int:
    with env["db"].connection() as conn:
        return int(conn.execute(select(func.count()).select_from(collector_group_failures_table)).scalar_one())


@pytest.mark.parametrize("mode", ["returned_error", "noncanonical"])
@pytest.mark.parametrize("window", ["after_verdict", "after_first_terminal", "before_release"])
def test_a_recorded_collector_verdict_is_completed_on_resume_without_the_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, window: str
) -> None:
    control_env = build_pipeline(tmp_path / "control", _COLLECTOR_PIPELINE, _DOCS)
    control_calls = _flip_first_call(monkeypatch, mode)
    control = run_pipeline(control_env)
    assert control.status is RunStatus.FAILED
    assert control.collector_groups_failed == 1
    assert load_run_accounting_from_db(control_env["db"], landscape_run_id=control.run_id).collector_groups_failed == 1
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
    assert _collector_group_failure_count(env) == 1
    assert ("blocked", 3) in _journal_statuses(env)

    resumed = resume_pipeline(env)

    assert len(calls) == 1, "a recorded collector failure must not re-invoke the plugin"
    assert resumed.status is RunStatus.FAILED
    assert resumed.collector_groups_failed == 1
    assert load_run_accounting_from_db(env["db"], landscape_run_id=resumed.run_id).collector_groups_failed == 1
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
    assert _collector_group_failure_count(env) == 0

    resumed = resume_pipeline(env)

    # No verdict was recorded, so the flush re-runs; this plugin succeeds the second time.
    assert len(calls) == 2
    assert calls[1] == calls[0]
    assert resumed.status is RunStatus.COMPLETED
    assert resumed.collector_groups_failed == 0
    assert load_run_accounting_from_db(env["db"], landscape_run_id=resumed.run_id).collector_groups_failed == 0
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
    assert resumed.collector_groups_failed == 1
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


# The lost-members arm: a transform inside the scope drops one member (a zero
# divisor is a row error, discarded), the other two arrive, and the require_all
# roster closes with a loss. The plugin never runs; the verdict has no flush
# state, only the two arrived members' holds.
_LOSSY_PIPELINE = _COLLECTOR_PIPELINE.replace(
    """    on_success: pages
    on_error: discard
    options:
      array_field: items""",
    """    on_success: exploded
    on_error: discard
    options:
      array_field: items""",
).replace(
    "collectors:",
    """  - name: invert
    plugin: value_transform
    input: exploded
    on_success: pages
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: inverse
          expression: "1000 / row['item']"
collectors:""",
)
_LOSSY_DOCS = [{"id": 1, "items": [5, 0, 2]}]


@pytest.mark.parametrize("window", ["after_verdict", "before_release"])
def test_a_recorded_lost_members_verdict_is_completed_once_on_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: str) -> None:
    """The flush-less arm: resume completes the recorded verdict, and the loss replay does not fail the group twice."""
    assert "invert" in _LOSSY_PIPELINE and "on_success: exploded" in _LOSSY_PIPELINE  # the rewrite landed
    control_env = build_pipeline(tmp_path / "control", _LOSSY_PIPELINE, _LOSSY_DOCS)
    control_calls = _count_calls(monkeypatch)
    control = run_pipeline(control_env)
    assert control_calls == [], "a require_all group with a lost member never invokes the plugin"

    env = build_pipeline(tmp_path / "crashed", _LOSSY_PIPELINE, _LOSSY_DOCS)
    calls = _count_calls(monkeypatch)
    with monkeypatch.context() as crash_patch:
        fired = _inject(crash_patch, window)
        with pytest.raises(_Crash):
            run_pipeline(env)
    assert fired == [window]
    # Two arrived members, both holds FAILED by the one verdict (no flush state exists).
    assert _collector_hold_statuses(env) == [NodeStateStatus.FAILED.value] * 2

    resumed = resume_pipeline(env)

    assert calls == []
    assert resumed.status is control.status
    assert terminal_counts(env["db"]) == terminal_counts(control_env["db"])
    assert _journal_statuses(env) == _journal_statuses(control_env)
