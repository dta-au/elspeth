# tests/integration/pipeline/test_batch_flush_recovery_and_redaction.py
"""Failed batch flushes survive crashes (elspeth-5887fb7928 engine review; operator ruling 2026-09-23).

Two kinds of failed flush reach a resume, and crash timing must not change
either one's outcome:

- The flush died BEFORE its verdict was recorded: the plugin raised, or the
  process died inside the verdict transaction. Nothing final exists, so
  resume retries the batch. A retried flush that dies too is retried again,
  and ``handle_incomplete_batches`` returns the chain ``{A: B, B: C}``; the
  journal restore follows it to C (``resolve_retry_chain``) — a single lookup
  would land on the dead B and strand every member BLOCKED.
- The batch transform RETURNED an error and its FAILED verdict committed
  (``ExecutionRepository.complete_aggregation_failure``, ONE transaction:
  transform_errors rows, DIVERT, FAILED state and batch). The verdict is final:
  resume completes its disposition — the named on_error sink or the discard
  pair, with the recorded reason — and never re-invokes the plugin, even one
  that would succeed if asked again.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import Determinism, PipelineRow, RunStatus
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings
from elspeth.core.landscape import execution_repository
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    batches_table,
    node_states_table,
    routing_events_table,
    token_outcomes_table,
    token_work_items_table,
    transform_errors_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.executors.aggregation import AggregationExecutor
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.processor import RowProcessor
from elspeth.mcp.analyzers.reports import get_error_analysis, get_run_summary
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.web.execution.discard_summary import load_discard_summaries_from_db
from elspeth.web.execution.failure_samples import load_top_failure_categories
from tests.fixtures.plugins import CollectSink
from tests.integration.pipeline.test_aggregation_recovery import (
    _build_eof_aggregation_pipeline,
    _FailBatchTransform,
    _LoadCountingSource,
    _SumBatchTransform,
)

_ROWS = [{"value": 10}, {"value": 20}, {"value": 30}]


def _pipeline(tmp_path: Any, transform: Any, *, error_sink: CollectSink | None, db: LandscapeDB | None = None) -> dict[str, Any]:
    """The EOF aggregation pipeline, on SQLite unless ``db`` (the PostgreSQL proof) is given."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    if db is None:
        db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    checkpoint_mgr = CheckpointManager(db)
    source = _LoadCountingSource(list(_ROWS), on_success="batch_in")
    output_sink = CollectSink("output")
    config, graph = _build_eof_aggregation_pipeline(source, transform, output_sink, error_sink=error_sink)
    orchestrator = Orchestrator(
        db=db,
        checkpoint_manager=checkpoint_mgr,
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    )
    return {
        "db": db,
        "checkpoint_mgr": checkpoint_mgr,
        "payload_store": FilesystemPayloadStore(tmp_path / "payloads"),
        "source": source,
        "output_sink": output_sink,
        "config": config,
        "graph": graph,
        "orchestrator": orchestrator,
    }


def _run_id(db: LandscapeDB) -> str:
    with db.connection() as conn:
        return str(conn.execute(select(batches_table.c.run_id)).scalars().first())


def _resume(env: dict[str, Any], run_id: str) -> Any:
    recovery = RecoveryManager(env["db"], env["checkpoint_mgr"])
    check = recovery.can_resume(run_id, env["graph"])
    assert check.can_resume, f"Expected resumable run, got: {check.reason}"
    resume_point = recovery.get_resume_point(run_id, env["graph"])
    assert resume_point is not None
    return env["orchestrator"].resume(
        resume_point=resume_point, config=env["config"], graph=env["graph"], payload_store=env["payload_store"]
    )


def _audit(db: LandscapeDB, run_id: str) -> dict[str, Any]:
    with db.connection() as conn:
        return {
            "terminals": conn.execute(
                select(token_outcomes_table.c.token_id, token_outcomes_table.c.path, token_outcomes_table.c.sink_name)
                .where(token_outcomes_table.c.run_id == run_id)
                .where(token_outcomes_table.c.completed == 1)
            ).all(),
            "batch_statuses": sorted(conn.execute(select(batches_table.c.status).where(batches_table.c.run_id == run_id)).scalars()),
            "transform_error_count": len(
                conn.execute(select(transform_errors_table.c.error_id).where(transform_errors_table.c.run_id == run_id)).all()
            ),
            "work_statuses": set(
                conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == run_id)).scalars()
            ),
        }


def _outcome_evidence(db: LandscapeDB, run_id: str) -> dict[str, Any]:
    """Everything a failed batch's disposition decides, free of per-run ids.

    Terminal outcomes as (outcome, path, sink, error_hash); the transform_errors
    rows as (reason text, destination); the FAILED flush states' reasons; the
    DIVERT count; the batch statuses.
    """
    with db.connection() as conn:
        return {
            "terminals": Counter(
                (row.outcome, row.path, row.sink_name, row.error_hash)
                for row in conn.execute(
                    select(
                        token_outcomes_table.c.outcome,
                        token_outcomes_table.c.path,
                        token_outcomes_table.c.sink_name,
                        token_outcomes_table.c.error_hash,
                    )
                    .where(token_outcomes_table.c.run_id == run_id)
                    .where(token_outcomes_table.c.completed == 1)
                )
            ),
            "transform_errors": Counter(
                (row.error_details_json, row.destination)
                for row in conn.execute(
                    select(transform_errors_table.c.error_details_json, transform_errors_table.c.destination).where(
                        transform_errors_table.c.run_id == run_id
                    )
                )
            ),
            "failed_state_reasons": sorted(
                conn.execute(
                    select(node_states_table.c.error_json)
                    .where(node_states_table.c.run_id == run_id)
                    .where(node_states_table.c.status == "failed")
                ).scalars()
            ),
            "routing_events": len(
                conn.execute(
                    select(routing_events_table.c.event_id)
                    .select_from(
                        routing_events_table.join(node_states_table, routing_events_table.c.state_id == node_states_table.c.state_id)
                    )
                    .where(node_states_table.c.run_id == run_id)
                ).all()
            ),
            "batch_statuses": sorted(conn.execute(select(batches_table.c.status).where(batches_table.c.run_id == run_id)).scalars()),
        }


def _run_result_fields(result: Any) -> dict[str, Any]:
    return {
        "status": result.status,
        "rows_processed": result.rows_processed,
        "rows_succeeded": result.rows_succeeded,
        "rows_failed": result.rows_failed,
        "rows_routed_success": result.rows_routed_success,
        "rows_routed_failure": result.rows_routed_failure,
        "rows_quarantined": result.rows_quarantined,
        "routed_destinations": dict(result.routed_destinations),
    }


def _crash_sequence(points: Sequence[tuple[type, str, Any, bool]], crashes: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise ``RuntimeError("injected crash")`` ``crashes`` times across ``points``.

    Each point is ``(cls, method, original, after)``: ``original`` is the real
    function ``cls.method`` is replaced over. Crash k lands on
    ``points[min(k, len(points) - 1)]`` — the first crash on the first point,
    every later one on the last — after the real method ran when that point
    says ``after``. Every call past the budget runs untouched.
    """
    raised = [0]
    for index, (cls, method, original, after) in enumerate(points):

        def crash(self: Any, *args: Any, _original: Any = original, _index: int = index, _after: bool = after, **kwargs: Any) -> Any:
            if raised[0] >= crashes or min(raised[0], len(points) - 1) != _index:
                return _original(self, *args, **kwargs)
            if _after:
                _original(self, *args, **kwargs)
            raised[0] += 1
            raise RuntimeError("injected crash")

        monkeypatch.setattr(cls, method, crash)


def _crash_inside_the_verdict_transaction(crashes: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Die after the verdict's transform_errors INSERT ran, INSIDE its transaction.

    ``complete_aggregation_failure`` then rolls the whole verdict back: no
    transform_errors row, no DIVERT, no FAILED-with-reason state survives. The
    flush's own cleanup marks the state and batch failed as a crash, exactly
    as for a plugin that raised.
    """
    original = execution_repository.insert_batch_transform_errors_on
    raised = [0]

    def crash(*args: Any, **kwargs: Any) -> Any:
        written = original(*args, **kwargs)
        if raised[0] < crashes:
            raised[0] += 1
            raise RuntimeError("injected crash")
        return written

    monkeypatch.setattr(execution_repository, "insert_batch_transform_errors_on", crash)


class _FailOnceThenSumBatchTransform(_SumBatchTransform):
    """Returns a batch error on its first flush, then sums: asked again, it would succeed."""

    name = "fail_once_then_sum"
    determinism = Determinism.NON_DETERMINISTIC

    def __init__(self) -> None:
        super().__init__()
        self._failed_once = False

    def process(self, row: PipelineRow | list[PipelineRow], ctx: Any) -> TransformResult:
        if isinstance(row, list) and not self._failed_once:
            self._failed_once = True
            self.batch_calls += 1
            return TransformResult.error({"reason": "batch_failed", "error": "transient"})
        return super().process(row, ctx)


# Where the FIRST crash lands once the verdict is recorded. Every later crash
# (one per resume) lands where the resume's disposition would hand the members
# on, before its complete_barrier.
_VERDICT_WINDOWS = [
    # The verdict transaction committed and the executor returned; the
    # processor never saw the result.
    pytest.param(
        [
            (AggregationExecutor, "execute_flush", AggregationExecutor.execute_flush, True),
            (RowProcessor, "_dispose_failed_flush", RowProcessor._dispose_failed_flush, False),
        ],
        id="after_the_verdict_committed",
    ),
    # The disposition was planned; its complete_barrier (the BLOCKED release)
    # never ran.
    pytest.param(
        [(RowProcessor, "_complete_aggregation_flush", RowProcessor._complete_aggregation_flush, False)],
        id="before_the_barrier_handoff",
    ),
]


@pytest.mark.timeout(240)
class TestARecordedFailedVerdictIsFinal:
    """Operator ruling 2026-09-23: resume completes a recorded FAILED verdict and never re-runs its batch."""

    @pytest.mark.parametrize("crashes", [1, 2, 3])
    @pytest.mark.parametrize("on_error", ["quarantine", "discard"])
    @pytest.mark.parametrize("points", _VERDICT_WINDOWS)
    def test_resume_completes_the_recorded_verdict_and_never_reinvokes_the_plugin(
        self,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
        points: list[tuple[type, str, Any, bool]],
        on_error: str,
        crashes: int,
    ) -> None:
        # The no-crash run of the same pipeline is the oracle.
        control_error_sink = CollectSink("quarantine") if on_error == "quarantine" else None
        control = _pipeline(tmp_path / "control", _FailOnceThenSumBatchTransform(), error_sink=control_error_sink)
        control_result = control["orchestrator"].run(control["config"], graph=control["graph"], payload_store=control["payload_store"])
        control_run_id = _run_id(control["db"])

        transform = _FailOnceThenSumBatchTransform()
        error_sink = CollectSink("quarantine") if on_error == "quarantine" else None
        env = _pipeline(tmp_path / "crashed", transform, error_sink=error_sink)
        _crash_sequence(points, crashes, monkeypatch)

        with pytest.raises(RuntimeError, match="injected crash"):
            env["orchestrator"].run(env["config"], graph=env["graph"], payload_store=env["payload_store"])
        run_id = _run_id(env["db"])
        for _ in range(crashes - 1):
            with pytest.raises(RuntimeError, match="injected crash"):
                _resume(env, run_id)
            audit = _audit(env["db"], run_id)
            assert audit["work_statuses"] == {"blocked"}, "a crashed disposition leaves every member BLOCKED"
            assert audit["batch_statuses"] == ["failed"], "the verdict is never retried into a DRAFT"
        result = _resume(env, run_id)

        assert transform.batch_calls == 1, "the plugin decided once; asked again it would have succeeded"
        assert env["source"].load_invocations == 1
        assert env["output_sink"].results == [] == control["output_sink"].results
        if error_sink is not None:
            assert control_error_sink is not None
            assert error_sink.results == _ROWS == control_error_sink.results, "every member reaches the sink exactly once, in order"
        assert _run_result_fields(result) == _run_result_fields(control_result)
        evidence = _outcome_evidence(env["db"], run_id)
        control_evidence = _outcome_evidence(control["db"], control_run_id)
        failed_states = evidence.pop("failed_state_reasons")
        control_failed_states = control_evidence.pop("failed_state_reasons")
        assert evidence == control_evidence, "the recorded verdict's disposition is the no-crash disposition"
        assert sum(evidence["terminals"].values()) == 3
        assert evidence["transform_errors"] == Counter({('{"error":"transient","reason":"batch_failed"}', on_error): 3})
        assert evidence["batch_statuses"] == ["failed"]
        # The crash window adds the orchestrator's own crash bookkeeping, never a
        # second verdict: exactly one FAILED state carries the batch reason.
        assert failed_states.count('{"error":"transient","reason":"batch_failed"}') == 1
        assert control_failed_states == ['{"error":"transient","reason":"batch_failed"}']
        assert _audit(env["db"], run_id)["work_statuses"] == {"terminal"}


@pytest.mark.timeout(240)
class TestAFlushWithoutARecordedVerdictReruns:
    """A flush that died before its verdict committed has no verdict: resume re-runs it."""

    @pytest.mark.parametrize("crashes", [1, 2, 3])
    @pytest.mark.parametrize("on_error", ["quarantine", "discard"])
    def test_a_crash_inside_the_verdict_transaction_leaves_nothing_and_the_flush_reruns(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, on_error: str, crashes: int
    ) -> None:
        error_sink = CollectSink("quarantine") if on_error == "quarantine" else None
        transform = _FailBatchTransform()
        env = _pipeline(tmp_path, transform, error_sink=error_sink)
        _crash_inside_the_verdict_transaction(crashes, monkeypatch)

        with pytest.raises(RuntimeError, match="injected crash"):
            env["orchestrator"].run(env["config"], graph=env["graph"], payload_store=env["payload_store"])
        run_id = _run_id(env["db"])
        evidence = _outcome_evidence(env["db"], run_id)
        assert evidence["transform_errors"] == Counter(), "the rolled-back verdict left no transform_errors row"
        assert evidence["routing_events"] == 0, "... and no DIVERT"
        for _ in range(crashes - 1):
            with pytest.raises(RuntimeError, match="injected crash"):
                _resume(env, run_id)
        result = _resume(env, run_id)

        attempts = crashes + 1
        assert transform.batch_calls == attempts, "each attempt without a verdict re-ran the batch"
        if error_sink is not None:
            assert error_sink.results == _ROWS
            assert result.status == RunStatus.FAILED
            assert result.rows_routed_failure == 3
        else:
            assert result.status == RunStatus.COMPLETED_WITH_FAILURES
            assert result.rows_quarantined == 3
        evidence = _outcome_evidence(env["db"], run_id)
        assert sum(evidence["terminals"].values()) == 3
        assert evidence["transform_errors"] == Counter(
            {('{"error":"injected batch flush failure","reason":"batch_failed"}', on_error): 3}
        ), "only the committed verdict's rows exist"
        assert evidence["routing_events"] == (1 if error_sink is not None else 0)
        assert evidence["batch_statuses"] == ["failed"] * attempts, "one FAILED batch per attempt; only the last holds the verdict"
        assert _audit(env["db"], run_id)["work_statuses"] == {"terminal"}

    def test_a_rolled_back_verdict_is_decided_again_and_the_rerun_may_succeed(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """No verdict was recorded, so the plugin is asked again — and this time it sums.

        The readers that answer "how many tokens failed or were discarded here"
        derive from terminal outcomes, and no transform_errors row survived the
        rollback, so every one of them reports nothing.
        """
        transform = _FailOnceThenSumBatchTransform()
        env = _pipeline(tmp_path, transform, error_sink=None)
        _crash_inside_the_verdict_transaction(1, monkeypatch)
        with pytest.raises(RuntimeError, match="injected crash"):
            env["orchestrator"].run(env["config"], graph=env["graph"], payload_store=env["payload_store"])
        run_id = _run_id(env["db"])
        result = _resume(env, run_id)

        db = env["db"]
        audit = _audit(db, run_id)
        assert result.status == RunStatus.COMPLETED
        assert env["output_sink"].results == [{"value": 60, "count": 3}]
        assert transform.batch_calls == 2
        assert audit["transform_error_count"] == 0
        assert audit["batch_statuses"] == ["completed", "failed"]
        assert {path for _t, path, _s in audit["terminals"]} == {"batch_consumed", "default_flow"}
        assert load_discard_summaries_from_db(db, [run_id]) == {}
        assert load_top_failure_categories(db, run_id) == []
        factory = RecorderFactory(db)
        run_summary = get_run_summary(db, factory, run_id)
        assert "error" not in run_summary
        assert run_summary["errors"]["transform"] == 0
        error_analysis = get_error_analysis(db, factory, run_id)
        assert "error" not in error_analysis
        assert error_analysis["transform_errors"]["total"] == 0


class _CrashingSumBatchTransform(_SumBatchTransform):
    """Sums its batch, but the batch ``process()`` raises on its first ``crashes`` calls.

    A raising plugin aborts the run with the batch still EXECUTING — the path
    that existed before any failed batch was routed.
    """

    name = "crashing_sum_batch"
    determinism = Determinism.DETERMINISTIC

    def __init__(self, crashes: int) -> None:
        super().__init__()
        self._crashes_left = crashes

    def process(self, row: PipelineRow | list[PipelineRow], ctx: Any) -> TransformResult:
        if isinstance(row, list) and self._crashes_left > 0:
            self._crashes_left -= 1
            self.batch_calls += 1
            raise RuntimeError("injected crash")
        return super().process(row, ctx)


@pytest.mark.timeout(180)
@pytest.mark.parametrize("crashes", [1, 2, 3])
def test_a_successful_flush_completes_after_the_plugin_crashed_on_every_earlier_attempt(tmp_path: Any, crashes: int) -> None:
    """The single-hop remap predates the failed-batch route: two crashes inside
    a SUCCESSFUL flush strand every member too. Pinned here so the chain
    resolution is proved on the path every aggregation takes — a raising
    plugin records no verdict, so each resume retries."""
    transform = _CrashingSumBatchTransform(crashes)
    env = _pipeline(tmp_path, transform, error_sink=None)

    with pytest.raises(RuntimeError, match="injected crash"):
        env["orchestrator"].run(env["config"], graph=env["graph"], payload_store=env["payload_store"])
    run_id = _run_id(env["db"])
    for _ in range(crashes - 1):
        with pytest.raises(RuntimeError, match="injected crash"):
            _resume(env, run_id)

    result = _resume(env, run_id)

    assert result.status == RunStatus.COMPLETED
    assert env["output_sink"].results == [{"value": 60, "count": 3}]
    assert transform.batch_calls == crashes + 1
    audit = _audit(env["db"], run_id)
    assert audit["work_statuses"] == {"terminal"}
    assert audit["batch_statuses"] == ["completed"] + ["failed"] * crashes


class _RaiseThenFailThenSumBatchTransform(_SumBatchTransform):
    """Raises on its first flush (no verdict), returns a batch error on the second, sums after."""

    name = "raise_then_fail_then_sum"
    determinism = Determinism.NON_DETERMINISTIC

    def process(self, row: PipelineRow | list[PipelineRow], ctx: Any) -> TransformResult:
        if isinstance(row, list) and self.batch_calls < 2:
            self.batch_calls += 1
            if self.batch_calls == 1:
                raise RuntimeError("injected crash")
            return TransformResult.error({"reason": "batch_failed", "error": "transient"})
        return super().process(row, ctx)


@pytest.mark.timeout(240)
@pytest.mark.parametrize("on_error", ["quarantine", "discard"])
def test_a_verdict_recorded_on_a_retry_is_completed_through_the_retry_chain(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, on_error: str
) -> None:
    """The retry chain and the final verdict in one resume.

    Attempt 1 (batch A) raises: no verdict, so the first resume retries A as B.
    Attempt 2 (batch B) returns an error: B's verdict commits, then the run
    dies before B's disposition. The second resume keeps A's chain edge (A is
    retried already, so its idempotent retry returns B), never retries B, and
    completes B's recorded verdict from its members' BLOCKED rows — the plugin,
    which would now succeed, is not called a third time.
    """
    error_sink = CollectSink("quarantine") if on_error == "quarantine" else None
    transform = _RaiseThenFailThenSumBatchTransform()
    env = _pipeline(tmp_path, transform, error_sink=error_sink)

    with pytest.raises(RuntimeError, match="injected crash"):
        env["orchestrator"].run(env["config"], graph=env["graph"], payload_store=env["payload_store"])
    run_id = _run_id(env["db"])
    _crash_sequence([(RowProcessor, "_complete_aggregation_flush", RowProcessor._complete_aggregation_flush, False)], 1, monkeypatch)
    with pytest.raises(RuntimeError, match="injected crash"):
        _resume(env, run_id)
    assert transform.batch_calls == 2
    assert _audit(env["db"], run_id)["work_statuses"] == {"blocked"}
    result = _resume(env, run_id)

    assert transform.batch_calls == 2, "the recorded verdict on the retry is completed, never re-run"
    assert env["output_sink"].results == []
    if error_sink is not None:
        assert error_sink.results == _ROWS
        assert result.status == RunStatus.FAILED
    else:
        assert result.status == RunStatus.COMPLETED_WITH_FAILURES
        assert result.rows_quarantined == 3
    evidence = _outcome_evidence(env["db"], run_id)
    assert sum(evidence["terminals"].values()) == 3
    assert evidence["transform_errors"] == Counter({('{"error":"transient","reason":"batch_failed"}', on_error): 3})
    assert evidence["routing_events"] == (1 if error_sink is not None else 0)
    assert evidence["batch_statuses"] == ["failed", "failed"], "A (crashed, retried) and B (the verdict); no third batch"
    assert _audit(env["db"], run_id)["work_statuses"] == {"terminal"}


@pytest.mark.timeout(180)
@pytest.mark.parametrize("on_error", ["quarantine", "discard"])
def test_every_reader_counts_each_failed_token_once_after_a_crash_after_the_verdict(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, on_error: str
) -> None:
    """The verdict is written once, however the run crashed (elspeth-5887fb7928 E8, C4).

    Every reader that answers "how many tokens failed here" says 3: the web
    discard summary, the run-failure category summary, and the MCP run summary
    and error analysis.
    """
    error_sink = CollectSink("quarantine") if on_error == "quarantine" else None
    env = _pipeline(tmp_path, _FailBatchTransform(), error_sink=error_sink)
    _crash_sequence([(AggregationExecutor, "execute_flush", AggregationExecutor.execute_flush, True)], 1, monkeypatch)
    with pytest.raises(RuntimeError, match="injected crash"):
        env["orchestrator"].run(env["config"], graph=env["graph"], payload_store=env["payload_store"])
    run_id = _run_id(env["db"])
    _resume(env, run_id)

    db = env["db"]
    audit = _audit(db, run_id)
    assert audit["transform_error_count"] == 3, "one verdict, one row per member"
    assert len(audit["terminals"]) == len({token_id for token_id, _p, _s in audit["terminals"]}) == 3
    assert audit["work_statuses"] == {"terminal"}

    discard_summaries = load_discard_summaries_from_db(db, [run_id])
    if on_error == "discard":
        assert discard_summaries[run_id].transform_errors == 3
    else:
        assert discard_summaries == {}, "a routed batch discards nothing"
    assert [summary.count for summary in load_top_failure_categories(db, run_id)] == [3]
    factory = RecorderFactory(db)
    run_summary = get_run_summary(db, factory, run_id)
    assert "error" not in run_summary
    assert run_summary["errors"]["transform"] == 3
    error_analysis = get_error_analysis(db, factory, run_id)
    assert "error" not in error_analysis
    assert error_analysis["transform_errors"]["total"] == 3
    assert [group["count"] for group in error_analysis["transform_errors"]["by_transform"]] == [3]
