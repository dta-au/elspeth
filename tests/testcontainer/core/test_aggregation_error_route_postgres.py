"""PostgreSQL proof of a failed aggregation batch's disposition (elspeth-d2e3f29d10).

The SQLite end-to-end tests in
``tests/integration/pipeline/test_row_type_violation_routing.py`` prove the
semantics. This file runs the same production pipeline against PostgreSQL,
because the writes it adds only exist there as SQL the default selection never
sends to PostgreSQL:

- the per-member ``transform_errors`` executemany INSERT ... RETURNING
  (``insert_batch_transform_errors_on``, operator ruling B5), inside the
  batch's one FAILED-verdict transaction (``complete_aggregation_failure``);
- the named-sink arm's BLOCKED -> PENDING_SINK ``complete_barrier`` handoff
  carrying ``pending_error_hash``/``pending_error_message`` (the CHECK arm
  on ``token_work_items``);
- the discard arm's (FAILURE, QUARANTINED_AT_SOURCE) terminals written inside
  that same transaction (operator ruling B3);
- resume of a recorded FAILED verdict (operator ruling 2026-09-23): the
  correlated verdict predicate (``recorded_failure_verdict_condition``) that
  keeps the batch out of ``get_incomplete_batches`` and hands its BLOCKED
  members to ``list_recorded_aggregation_failures``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.fixtures.plugins import CollectSink
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.pipeline.test_batch_flush_recovery_and_redaction import (
    _crash_sequence,
    _FailOnceThenSumBatchTransform,
    _outcome_evidence,
    _pipeline,
    _resume,
    _run_id,
)
from tests.integration.pipeline.test_row_type_violation_routing import (
    _CSV_ROWS,
    _failed_flush_audit,
    _run_csv_batch_stats_pipeline,
)

from elspeth.contracts import RunStatus, TerminalOutcome, TerminalPath
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.processor import RowProcessor
from elspeth.mcp.analyzers.reports import get_error_analysis, get_run_summary
from elspeth.web.execution.discard_summary import load_discard_summaries_from_db
from elspeth.web.execution.failure_samples import load_top_failure_categories

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


@pytest.mark.parametrize(
    ("on_error", "status", "pair", "routing_events"),
    [
        pytest.param(
            "quarantine",
            RunStatus.FAILED,
            (TerminalOutcome.FAILURE.value, TerminalPath.ON_ERROR_ROUTED.value, "quarantine"),
            1,
            id="named-sink",
        ),
        pytest.param(
            "discard",
            RunStatus.COMPLETED_WITH_FAILURES,
            (TerminalOutcome.FAILURE.value, TerminalPath.QUARANTINED_AT_SOURCE.value, None),
            0,
            id="discard",
        ),
    ],
)
def test_failed_batch_disposition_on_postgres(
    postgres_url: str,
    tmp_path: Path,
    on_error: str,
    status: RunStatus,
    pair: tuple[str, str, str | None],
    routing_events: int,
) -> None:
    db = LandscapeDB.from_url(postgres_url)
    assert db.engine.dialect.name == "postgresql"
    try:
        result, db, output_rows, quarantine_rows = _run_csv_batch_stats_pipeline(tmp_path, on_error=on_error, trigger="count", db=db)

        assert result.status is status
        assert output_rows == []
        expected_quarantine = [{"id": row_id, "amount": amount} for row_id, amount in _CSV_ROWS] if on_error == "quarantine" else []
        assert quarantine_rows == expected_quarantine

        audit = _failed_flush_audit(db, result.run_id)
        assert len(audit["outcomes"]) == 3
        assert {outcome.token_id for outcome in audit["outcomes"]} == audit["row_token_ids"]
        assert {(outcome.outcome, outcome.path, outcome.sink_name) for outcome in audit["outcomes"]} == {pair}
        assert len(audit["routing"]) == routing_events
        assert len(audit["transform_errors"]) == 3
        assert {row.destination for row in audit["transform_errors"]} == {on_error}
        [batch] = audit["batches"]
        assert batch.status == "failed"

        # The counting readers (elspeth-5887fb7928 ruling: counts derive from
        # terminal outcomes) join transform_errors to token_outcomes with a
        # correlated latest-row subquery and a NOT EXISTS over node_states.
        # Only here does that SQL reach PostgreSQL.
        discard_summaries = load_discard_summaries_from_db(db, [result.run_id])
        if on_error == "discard":
            assert discard_summaries[result.run_id].transform_errors == 3
        else:
            assert discard_summaries == {}, "a routed batch failed but discarded nothing"
        assert [summary.count for summary in load_top_failure_categories(db, result.run_id)] == [3]
        factory = RecorderFactory(db)
        run_summary: Any = get_run_summary(db, factory, result.run_id)
        assert run_summary["errors"]["transform"] == 3
        error_analysis: Any = get_error_analysis(db, factory, result.run_id)
        assert error_analysis["transform_errors"]["total"] == 3
    finally:
        db.close()


@pytest.mark.parametrize("on_error", ["quarantine", "discard"])
def test_resume_completes_a_recorded_verdict_on_postgres(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on_error: str) -> None:
    """Crash after the verdict committed, twice: resume completes it without re-invoking the plugin."""
    with postgres_test_target(driver="psycopg") as fresh_url:
        db = LandscapeDB.from_url(fresh_url)
        assert db.engine.dialect.name == "postgresql"
        try:
            error_sink = CollectSink("quarantine") if on_error == "quarantine" else None
            transform = _FailOnceThenSumBatchTransform()
            env = _pipeline(tmp_path, transform, error_sink=error_sink, db=db)
            _crash_sequence(
                [(RowProcessor, "_complete_aggregation_flush", RowProcessor._complete_aggregation_flush, False)], 2, monkeypatch
            )

            with pytest.raises(RuntimeError, match="injected crash"):
                env["orchestrator"].run(env["config"], graph=env["graph"], payload_store=env["payload_store"])
            run_id = _run_id(db)
            with pytest.raises(RuntimeError, match="injected crash"):
                _resume(env, run_id)
            assert RecorderFactory(db).execution.get_incomplete_batches(run_id) == [], "the verdict is never retried"
            result = _resume(env, run_id)

            assert transform.batch_calls == 1
            assert result.status is (RunStatus.FAILED if error_sink is not None else RunStatus.COMPLETED_WITH_FAILURES)
            if error_sink is not None:
                assert error_sink.results == [{"value": 10}, {"value": 20}, {"value": 30}]
            evidence = _outcome_evidence(db, run_id)
            assert sum(evidence["terminals"].values()) == 3
            assert sum(evidence["transform_errors"].values()) == 3
            assert evidence["routing_events"] == (1 if error_sink is not None else 0)
            assert evidence["batch_statuses"] == ["failed"]
        finally:
            db.close()
