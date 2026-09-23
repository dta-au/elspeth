"""PostgreSQL proof of a failed aggregation batch's disposition (elspeth-d2e3f29d10).

The SQLite end-to-end tests in
``tests/integration/pipeline/test_row_type_violation_routing.py`` prove the
semantics. This file runs the same production pipeline against PostgreSQL,
because the writes it adds only exist there as SQL the default selection never
sends to PostgreSQL:

- the per-member ``transform_errors`` executemany INSERT ... RETURNING
  (``record_batch_transform_errors_leader``, operator ruling B5);
- the named-sink arm's BLOCKED -> PENDING_SINK ``complete_barrier`` handoff
  carrying ``pending_error_hash``/``pending_error_message`` (the CHECK arm
  on ``token_work_items``);
- the discard arm's (FAILURE, QUARANTINED_AT_SOURCE) terminals written inside
  that same transaction (operator ruling B3).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.pipeline.test_row_type_violation_routing import (
    _CSV_ROWS,
    _failed_flush_audit,
    _run_csv_batch_stats_pipeline,
)

from elspeth.contracts import RunStatus, TerminalOutcome, TerminalPath
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
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
