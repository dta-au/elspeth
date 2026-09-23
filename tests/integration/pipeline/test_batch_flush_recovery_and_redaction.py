# tests/integration/pipeline/test_batch_flush_recovery_and_redaction.py
"""Failed batch flushes survive REPEATED crashes (elspeth-5887fb7928 engine review).

A resume retries every dead aggregation batch; the members' BUFFERED outcomes
keep the ORIGINAL batch id. When the retried flush dies too, the next resume
retries the retry, and ``handle_incomplete_batches`` returns the chain
``{A: B, B: C}``. The journal restore must follow it to C: a single lookup
lands on the dead B, the flush dies on the immutable-terminal transition, and
every member stays BLOCKED with no terminal outcome on every later resume.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import Determinism, PipelineRow, RunStatus
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings
from elspeth.core.landscape.data_flow.errors import ErrorAuditRepository
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    batches_table,
    token_outcomes_table,
    token_work_items_table,
    transform_errors_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
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


def _pipeline(tmp_path: Any, transform: Any, *, error_sink: CollectSink | None) -> dict[str, Any]:
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


def _crash_first(cls: type, method: str, original: Any, crashes: int, *, after: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``cls.method`` (whose real function is ``original``) with one that
    raises on its first ``crashes`` calls — after doing its work when ``after``."""
    raised: list[bool] = []

    def crash(self: Any, *args: Any, **kwargs: Any) -> Any:
        if after:
            result = original(self, *args, **kwargs)
        if len(raised) < crashes:
            raised.append(True)
            raise RuntimeError("injected crash")
        return result if after else original(self, *args, **kwargs)

    monkeypatch.setattr(cls, method, crash)


@pytest.mark.timeout(180)
class TestRepeatedCrashInAFailedFlush:
    """The routed failed-flush windows, crashed on the original run AND on every resume but the last."""

    @pytest.mark.parametrize("crashes", [1, 2, 3])
    @pytest.mark.parametrize(
        ("window", "cls", "method", "original", "after"),
        [
            # The per-member transform_errors write has committed; the batch is still EXECUTING.
            pytest.param(
                "after_transform_errors",
                ErrorAuditRepository,
                "record_batch_transform_errors_leader",
                ErrorAuditRepository.record_batch_transform_errors_leader,
                True,
                id="after_transform_errors",
            ),
            # The executor finalized the FAILED batch; the BLOCKED members were never handed off.
            pytest.param(
                "before_barrier_handoff",
                RowProcessor,
                "_complete_aggregation_flush",
                RowProcessor._complete_aggregation_flush,
                False,
                id="before_barrier_handoff",
            ),
        ],
    )
    def test_every_member_is_routed_once_after_repeated_crashes(
        self,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
        crashes: int,
        window: str,
        cls: type,
        method: str,
        original: Any,
        after: bool,
    ) -> None:
        error_sink = CollectSink("quarantine")
        transform = _FailBatchTransform()
        env = _pipeline(tmp_path, transform, error_sink=error_sink)
        _crash_first(cls, method, original, crashes, after=after, monkeypatch=monkeypatch)

        with pytest.raises(RuntimeError, match="injected crash"):
            env["orchestrator"].run(env["config"], graph=env["graph"], payload_store=env["payload_store"])
        run_id = _run_id(env["db"])
        for _ in range(crashes - 1):
            with pytest.raises(RuntimeError, match="injected crash"):
                _resume(env, run_id)
            assert _audit(env["db"], run_id)["work_statuses"] == {"blocked"}, window

        result = _resume(env, run_id)

        attempts = crashes + 1
        assert result.status == RunStatus.FAILED
        assert result.rows_routed_failure == 3
        assert error_sink.results == _ROWS, "every member reaches the error sink exactly once, in order"
        assert env["output_sink"].results == []
        assert env["source"].load_invocations == 1
        assert transform.batch_calls == attempts, "each resume retries the batch exactly once"
        audit = _audit(env["db"], run_id)
        assert len(audit["terminals"]) == len({token_id for token_id, _p, _s in audit["terminals"]}) == 3
        assert {(path, sink) for _t, path, sink in audit["terminals"]} == {("on_error_routed", "quarantine")}
        assert audit["work_statuses"] == {"terminal"}, "nothing left BLOCKED"
        assert audit["batch_statuses"] == ["failed"] * attempts, "one FAILED batch per attempt, no stranded retry"
        assert audit["transform_error_count"] == 3 * attempts, "every attempt records its own per-member rows"


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
    resolution is proved on the path every aggregation takes."""
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


@pytest.mark.timeout(180)
@pytest.mark.parametrize("on_error", ["quarantine", "discard"])
def test_every_reader_counts_each_failed_token_once_after_a_crash_after_the_error_write(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, on_error: str
) -> None:
    """A resumed attempt writes the failed batch's transform_errors rows again (elspeth-5887fb7928 E8).

    Both attempts' rows stay as audit evidence (6 rows for 3 tokens), but every
    reader that answers "how many tokens failed here" must say 3: the web
    discard summary, the run-failure category summary, and the MCP run summary
    and error analysis.
    """
    error_sink = CollectSink("quarantine") if on_error == "quarantine" else None
    env = _pipeline(tmp_path, _FailBatchTransform(), error_sink=error_sink)
    _crash_first(
        ErrorAuditRepository,
        "record_batch_transform_errors_leader",
        ErrorAuditRepository.record_batch_transform_errors_leader,
        1,
        after=True,
        monkeypatch=monkeypatch,
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        env["orchestrator"].run(env["config"], graph=env["graph"], payload_store=env["payload_store"])
    run_id = _run_id(env["db"])
    _resume(env, run_id)

    db = env["db"]
    audit = _audit(db, run_id)
    assert audit["transform_error_count"] == 6, "control: each attempt recorded its own per-member rows"
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


class _FailOnceThenSumBatchTransform(_SumBatchTransform):
    """Returns a batch error on its first flush, then sums: a transient batch failure."""

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


@pytest.mark.timeout(180)
def test_no_reader_counts_a_failed_attempt_whose_resumed_retry_delivered_the_batch(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure and discard counts derive from terminal outcomes (elspeth-5887fb7928 ruling, review F3).

    The first flush fails and its members' transform_errors rows commit, and
    the process dies with the batch still EXECUTING. The resumed retry
    succeeds, so all three rows are delivered as one sum. The rows stay as
    attempt evidence, and no reader may report a failed or discarded token.

    Only this window is pinned. The ruling for a batch whose FAILED verdict was
    already recorded (the crash before the barrier handoff) is to complete
    that verdict on resume, so there the resumed plugin never runs.
    """
    transform = _FailOnceThenSumBatchTransform()
    env = _pipeline(tmp_path, transform, error_sink=None)
    _crash_first(
        ErrorAuditRepository,
        "record_batch_transform_errors_leader",
        ErrorAuditRepository.record_batch_transform_errors_leader,
        1,
        after=True,
        monkeypatch=monkeypatch,
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        env["orchestrator"].run(env["config"], graph=env["graph"], payload_store=env["payload_store"])
    run_id = _run_id(env["db"])
    result = _resume(env, run_id)

    db = env["db"]
    audit = _audit(db, run_id)
    assert result.status == RunStatus.COMPLETED
    assert env["output_sink"].results == [{"value": 60, "count": 3}]
    assert transform.batch_calls == 2
    assert audit["transform_error_count"] == 3, "control: the failed attempt's rows are attempt evidence in the audit trail"
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
    assert error_analysis["transform_errors"]["by_transform"] == []
    assert len(error_analysis["transform_errors"]["sample_details"]) == 3, "attempt evidence stays queryable"
