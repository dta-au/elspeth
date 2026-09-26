"""A multi-continuation aggregation release into a scope opener, on PostgreSQL.

The SQLite suite (``tests/integration/pipeline/test_aggregation_release_continuations.py``)
proves that an out-of-claim aggregation release advances all its continuations
in ONE drain. This is the PostgreSQL twin. The scheduler's idempotent enqueue
has its own ``ON CONFLICT DO NOTHING`` dialect branch, and the replay check it
feeds is what refused the per-continuation drain. Run with
``pytest -m testcontainer -n 0``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.pipeline.test_aggregation_release_continuations import (
    _DOCUMENTS,
    _count_collector_flushes,
    _fault_the_first_flush,
    _FlushFault,
    _journal_statuses,
    _output_rows,
    _scope_pipeline,
    _tokens_without_a_terminal_outcome,
)
from tests.integration.pipeline.test_barrier_hold_payload import build_pipeline, resume_pipeline, run_pipeline, terminal_counts

from elspeth.contracts.enums import RunStatus
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.landscape.database import LandscapeDB

pytestmark = pytest.mark.testcontainer


@pytest.mark.parametrize("output_mode", ["passthrough", "transform"])
def test_a_release_into_a_scope_opener_completes_on_postgres(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_mode: str) -> None:
    """Two documents: the uncrashed run completes, and a faulted flush resumes to the same outcome."""
    docs = _DOCUMENTS["two"]
    with postgres_test_target(driver="psycopg") as control_url, postgres_test_target(driver="psycopg") as faulted_url:
        control_db = LandscapeDB.from_url(control_url)
        db = LandscapeDB.from_url(faulted_url)
        assert db.engine.dialect.name == "postgresql"
        try:
            control_env = build_pipeline(tmp_path / "control", _scope_pipeline(output_mode), docs, db=control_db)
            assert run_pipeline(control_env).status is RunStatus.COMPLETED
            assert _tokens_without_a_terminal_outcome(control_db) == 0
            assert set(_journal_statuses(control_db)) == {TokenWorkStatus.TERMINAL.value}

            env = build_pipeline(tmp_path / "faulted", _scope_pipeline(output_mode), docs, db=db)
            flushes = _count_collector_flushes(monkeypatch)
            fired = _fault_the_first_flush(monkeypatch)
            with pytest.raises(_FlushFault):
                run_pipeline(env)
            assert fired == ["flush"]

            resumed = resume_pipeline(env)

            assert resumed.status is RunStatus.COMPLETED
            assert len(flushes) == 2
            assert _output_rows(env) == _output_rows(control_env)
            assert terminal_counts(db) == terminal_counts(control_db)
            assert _tokens_without_a_terminal_outcome(db) == 0
            assert set(_journal_statuses(db)) == {TokenWorkStatus.TERMINAL.value}
        finally:
            db.close()
            control_db.close()
